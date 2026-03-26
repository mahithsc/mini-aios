# Apollo Role Finder (People Search + Enrichment)

This is a small Python script that:
1) searches Apollo for people at a given company **domain** matching target title variants, then
2) optionally runs **bulk enrichment** on the selected candidates, and
3) exports results to CSV (and optionally JSON).

## Security
- **Do not paste your Apollo API key into chat**.
- Provide it as an environment variable: `APOLLO_API_KEY`.

## Install
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run (example)
```bash
export APOLLO_API_KEY='YOUR_KEY'

python apollo_role_finder.py \
  --domains 'example.com' \
  --roles '{
    "president": ["president", "ceo"],
    "head_support": ["head of customer support", "vp customer support", "director of customer support", "head of support"]
  }' \
  --top-k 3 \
  --enrich \
  --reveal-emails \
  --out apollo_people.csv \
  --out-json apollo_debug.json
```

## Notes
- People Search does **not** return emails/phones; enrichment does (plan/permissions dependent).
- The People Search endpoint requires a **master API key** (per Apollo docs).
- Bulk enrichment is limited to **10 people per call**.

## Endpoints used
- `POST https://api.apollo.io/api/v1/mixed_people/api_search`
- `POST https://api.apollo.io/api/v1/people/bulk_match`