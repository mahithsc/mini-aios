#!/usr/bin/env python3
"""Apollo.io role finder

Search Apollo for people at a given company domain (or domains) matching target titles,
optionally enrich results, and export to CSV/JSON.

Security note:
- DO NOT paste your API key into chat or hardcode it in source control.
- Provide it via the APOLLO_API_KEY environment variable.

Docs (for reference):
- People Search: https://docs.apollo.io/reference/people-api-search
- People Enrichment: https://docs.apollo.io/reference/people-enrichment

This script uses:
- POST https://api.apollo.io/api/v1/mixed_people/api_search
- POST https://api.apollo.io/api/v1/people/bulk_match

"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

BASE_URL = os.environ.get("APOLLO_BASE_URL", "https://api.apollo.io")
SEARCH_ENDPOINT = "/api/v1/mixed_people/api_search"
BULK_ENRICH_ENDPOINT = "/api/v1/people/bulk_match"


class ApolloError(RuntimeError):
    pass


def _env_api_key() -> str:
    key = os.environ.get("APOLLO_API_KEY")
    if not key:
        raise ApolloError(
            "Missing APOLLO_API_KEY environment variable. "
            "Set it like: export APOLLO_API_KEY='...'")
    return key


def _headers(api_key: str) -> Dict[str, str]:
    # Apollo docs show X-Api-Key; keep it explicit.
    return {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Cache-Control": "no-cache",
        "X-Api-Key": api_key,
    }


def _request_with_retries(
    session: requests.Session,
    method: str,
    url: str,
    headers: Dict[str, str],
    json_body: Optional[Dict[str, Any]] = None,
    timeout_s: int = 20,
    max_retries: int = 6,
) -> Dict[str, Any]:
    """Basic retry/backoff for 429/5xx."""

    backoff = 1.0
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = session.request(method, url, headers=headers, json=json_body, timeout=timeout_s)
        except requests.RequestException as e:
            last_err = e
            if attempt >= max_retries:
                raise ApolloError(f"Network error calling Apollo: {e}")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        if resp.status_code in (429, 500, 502, 503, 504):
            if attempt >= max_retries:
                raise ApolloError(
                    f"Apollo error {resp.status_code}: {resp.text[:500]}")
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    time.sleep(float(retry_after))
                except ValueError:
                    time.sleep(backoff)
            else:
                time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue

        if resp.status_code >= 400:
            raise ApolloError(f"Apollo error {resp.status_code}: {resp.text[:1000]}")

        try:
            return resp.json()
        except ValueError:
            raise ApolloError(f"Non-JSON response from Apollo ({resp.status_code}): {resp.text[:500]}")

    raise ApolloError(f"Failed after retries: {last_err}")


def normalize_title(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"\s+", " ", s)
    return s


def title_score(person_title: str, target_patterns: List[str]) -> int:
    """Score by presence of target phrases (case-insensitive)."""
    t = normalize_title(person_title or "")
    score = 0
    for p in target_patterns:
        if p in t:
            score += 10
    # Small boosts for seniority keywords
    for kw, pts in [("chief", 5), ("cxo", 5), ("president", 5), ("vp", 4), ("vice president", 4), ("head", 3), ("director", 2)]:
        if kw in t:
            score += pts
    return score


@dataclass
class PersonLite:
    id: str
    name: str
    title: str
    organization_name: Optional[str]
    organization_id: Optional[str]
    linkedin_url: Optional[str]
    city: Optional[str]
    state: Optional[str]
    country: Optional[str]
    has_email: Optional[bool]
    has_phone: Optional[Any]
    raw: Dict[str, Any]


def parse_people_search_response(data: Dict[str, Any]) -> Tuple[List[PersonLite], Dict[str, Any]]:
    people = []
    for p in data.get("people", []) or []:
        org = p.get("organization") or {}
        people.append(
            PersonLite(
                id=str(p.get("id") or ""),
                name=" ".join([x for x in [p.get("first_name"), p.get("last_name"), p.get("last_name_obfuscated")] if x]).strip(),
                title=p.get("title") or "",
                organization_name=org.get("name"),
                organization_id=str(org.get("id")) if org.get("id") else None,
                linkedin_url=p.get("linkedin_url") or p.get("linkedin") or None,
                city=p.get("city") or None,
                state=p.get("state") or None,
                country=p.get("country") or None,
                has_email=p.get("has_email"),
                has_phone=p.get("has_direct_phone") or p.get("has_phone") or None,
                raw=p,
            )
        )
    pagination = data.get("pagination") or {}
    return people, pagination


def apollo_people_search(
    session: requests.Session,
    api_key: str,
    domain: str,
    titles: List[str],
    person_locations: Optional[List[str]] = None,
    organization_locations: Optional[List[str]] = None,
    page: int = 1,
    per_page: int = 25,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{SEARCH_ENDPOINT}"

    # Per Apollo tutorial: filters are arrays of strings.
    payload: Dict[str, Any] = {
        "q_organization_domains_list": [domain],
        "person_titles": titles,
        "page": page,
        "per_page": per_page,
    }
    if person_locations:
        payload["person_locations"] = person_locations
    if organization_locations:
        payload["organization_locations"] = organization_locations

    return _request_with_retries(session, "POST", url, headers=_headers(api_key), json_body=payload)


def apollo_bulk_enrich(
    session: requests.Session,
    api_key: str,
    person_ids: List[str],
    reveal_personal_emails: bool = False,
    reveal_phone_number: bool = False,
    run_waterfall_email: bool = False,
    run_waterfall_phone: bool = False,
) -> Dict[str, Any]:
    url = f"{BASE_URL}{BULK_ENRICH_ENDPOINT}"

    details = [{"id": pid} for pid in person_ids]
    payload: Dict[str, Any] = {"details": details}

    # These params are documented on People Enrichment; Bulk endpoint shares the same params.
    # If your plan/key doesn’t allow reveal, Apollo will return without sensitive fields.
    payload["reveal_personal_emails"] = bool(reveal_personal_emails)
    payload["reveal_phone_number"] = bool(reveal_phone_number)
    payload["run_waterfall_email"] = bool(run_waterfall_email)
    payload["run_waterfall_phone"] = bool(run_waterfall_phone)

    return _request_with_retries(session, "POST", url, headers=_headers(api_key), json_body=payload)


def chunked(xs: List[str], n: int) -> Iterable[List[str]]:
    for i in range(0, len(xs), n):
        yield xs[i : i + n]


def choose_best_per_role(people: List[PersonLite], role: str, title_variants: List[str], top_k: int) -> List[PersonLite]:
    patterns = [normalize_title(v) for v in title_variants]
    scored = [(title_score(p.title, patterns), p) for p in people]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [p for s, p in scored if s > 0][:top_k]


def flatten_enriched_match(m: Dict[str, Any]) -> Dict[str, Any]:
    # Be conservative; Apollo’s response shape changes. Keep raw JSON too.
    out = {
        "id": m.get("id"),
        "name": m.get("name") or " ".join([x for x in [m.get("first_name"), m.get("last_name")] if x]).strip(),
        "title": m.get("title"),
        "linkedin_url": m.get("linkedin_url"),
        "headline": m.get("headline"),
        "email": m.get("email"),
        "email_status": m.get("email_status"),
        "sanitized_phone": None,
        "organization_name": m.get("organization_name"),
        "organization_id": m.get("organization_id"),
        "city": m.get("city"),
        "state": m.get("state"),
        "country": m.get("country"),
    }
    # Phones can appear under different keys; try a few.
    for k in ["mobile_phone", "direct_dial", "phone", "work_phone"]:
        if m.get(k):
            out["sanitized_phone"] = m.get(k)
            break
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Find people by title at a company domain using Apollo API")
    ap.add_argument("--domains", required=True, help="Comma-separated domains, e.g. 'acme.com,example.org'")
    ap.add_argument(
        "--roles",
        required=True,
        help=(
            "JSON mapping of role_name -> [title variants]. Example: "
            "'{" \
            "\"head_support\": [\"head of customer support\",\"vp customer support\"]," \
            "\"president\": [\"president\",\"ceo\"]" \
            "}'"
        ),
    )
    ap.add_argument("--person-locations", default=None, help="Comma-separated person locations filter")
    ap.add_argument("--organization-locations", default=None, help="Comma-separated org HQ locations filter")
    ap.add_argument("--per-page", type=int, default=25)
    ap.add_argument("--max-pages", type=int, default=10, help="Safety cap on pagination")
    ap.add_argument("--top-k", type=int, default=3, help="How many best candidates to keep per role")

    ap.add_argument("--enrich", action="store_true", help="Call bulk enrichment for selected candidates")
    ap.add_argument("--reveal-emails", action="store_true", help="Ask enrichment to reveal personal emails (plan-dependent)")
    ap.add_argument("--reveal-phone", action="store_true", help="Ask enrichment to reveal phone numbers (plan-dependent)")
    ap.add_argument("--waterfall-email", action="store_true", help="Run waterfall email (async via webhook if configured)")
    ap.add_argument("--waterfall-phone", action="store_true", help="Run waterfall phone (async via webhook if configured)")

    ap.add_argument("--out", default="apollo_people.csv", help="Output CSV path")
    ap.add_argument("--out-json", default=None, help="Optional output JSON path (raw selected/enriched)")

    args = ap.parse_args()

    api_key = _env_api_key()

    domains = [d.strip() for d in args.domains.split(",") if d.strip()]
    try:
        roles_map: Dict[str, List[str]] = json.loads(args.roles)
    except json.JSONDecodeError as e:
        raise ApolloError(f"--roles must be valid JSON: {e}")

    person_locations = [x.strip() for x in (args.person_locations or "").split(",") if x.strip()] or None
    org_locations = [x.strip() for x in (args.organization_locations or "").split(",") if x.strip()] or None

    with requests.Session() as session:
        selected_rows: List[Dict[str, Any]] = []
        raw_dump: Dict[str, Any] = {"selected": []}

        for domain in domains:
            # Pull enough search results for ranking.
            all_people: List[PersonLite] = []
            for page in range(1, args.max_pages + 1):
                data = apollo_people_search(
                    session=session,
                    api_key=api_key,
                    domain=domain,
                    titles=sorted({t for ts in roles_map.values() for t in ts}),
                    person_locations=person_locations,
                    organization_locations=org_locations,
                    page=page,
                    per_page=args.per_page,
                )
                people, pagination = parse_people_search_response(data)
                all_people.extend([p for p in people if p.id])

                total_pages = pagination.get("total_pages")
                if total_pages and page >= int(total_pages):
                    break
                # Stop early if Apollo returns less than per_page (common)
                if len(people) < args.per_page:
                    break

            # Choose best candidates for each role
            domain_selected: List[PersonLite] = []
            for role, variants in roles_map.items():
                best = choose_best_per_role(all_people, role=role, title_variants=variants, top_k=args.top_k)
                for p in best:
                    domain_selected.append(p)

            # De-dupe by person id
            uniq: Dict[str, PersonLite] = {}
            for p in domain_selected:
                uniq[p.id] = p
            domain_selected = list(uniq.values())

            raw_dump["selected"].append(
                {
                    "domain": domain,
                    "selected_people": [p.raw for p in domain_selected],
                }
            )

            if args.enrich and domain_selected:
                enriched_matches: List[Dict[str, Any]] = []
                for batch in chunked([p.id for p in domain_selected], 10):
                    enrich_data = apollo_bulk_enrich(
                        session=session,
                        api_key=api_key,
                        person_ids=batch,
                        reveal_personal_emails=args.reveal_emails,
                        reveal_phone_number=args.reveal_phone,
                        run_waterfall_email=args.waterfall_email,
                        run_waterfall_phone=args.waterfall_phone,
                    )
                    raw_dump.setdefault("enriched", []).append({"domain": domain, "response": enrich_data})
                    for m in enrich_data.get("matches", []) or []:
                        enriched_matches.append(m)

                # Write enriched rows
                for m in enriched_matches:
                    row = flatten_enriched_match(m)
                    row["domain"] = domain
                    selected_rows.append(row)
            else:
                # Write lite rows (no sensitive fields)
                for p in domain_selected:
                    selected_rows.append(
                        {
                            "domain": domain,
                            "id": p.id,
                            "name": p.name,
                            "title": p.title,
                            "linkedin_url": p.linkedin_url,
                            "organization_name": p.organization_name,
                            "organization_id": p.organization_id,
                            "city": p.city,
                            "state": p.state,
                            "country": p.country,
                            "has_email": p.has_email,
                            "has_phone": p.has_phone,
                        }
                    )

        # Output CSV
        if not selected_rows:
            print("No matches found.", file=sys.stderr)
            return 2

        # Column set
        cols = []
        for r in selected_rows:
            for k in r.keys():
                if k not in cols:
                    cols.append(k)

        with open(args.out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            for r in selected_rows:
                w.writerow(r)

        if args.out_json:
            with open(args.out_json, "w", encoding="utf-8") as f:
                json.dump(raw_dump, f, indent=2)

    print(f"Wrote {len(selected_rows)} rows to {args.out}")
    if args.out_json:
        print(f"Wrote raw JSON to {args.out_json}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApolloError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)