from __future__ import annotations

import json
import math
import os
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterator

from ..workspace import ensure_workspace_dir
from .toolcore import looks_binary, truncate_middle

_NOISE_DIRS = {
    ".git",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
    "venv",
}
_GENERATED_DIRS = {".next", "build", "coverage", "dist", "target"}
_SAFE_HIDDEN_FILES = {".aios-app.json", ".dockerignore", ".env.example", ".gitignore"}
_SENSITIVE_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "secrets.yaml",
    "secrets.yml",
}
_SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
_METADATA_FILES = {
    ".aios-app.json",
    "aios.deploy.yaml",
    "aios.deploy.yml",
    "package.json",
    "pyproject.toml",
    "readme.md",
    "workspace.md",
}
_STOP_WORDS = {
    "about", "again", "against", "also", "and", "app", "application",
    "are", "because", "been", "being", "blob", "blobs", "but", "can",
    "code", "content", "create", "does", "for", "from", "generated", "have", "into",
    "its", "just", "like", "llm", "more", "most", "not", "only", "other", "our",
    "out", "path", "relevant", "search", "should", "some", "than", "that", "the",
    "their", "then", "there", "these", "they", "this", "through", "tool", "tools",
    "update", "use", "using", "very", "want", "what", "when", "where", "which", "will", "with",
    "workspace", "would", "you", "your",
}
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]{1,127}")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_MAX_QUERY_CHARS = 50_000
_MAX_KEYWORDS = 32
_MAX_FILE_BYTES = 1_000_000
_MAX_SCAN_BYTES_PER_APP = 8_000_000
_MAX_EVIDENCE_PER_APP = 8
_MAX_READ_LINES = 500
_MAX_LINE_CHARS = 2_000


def _apps_root() -> Path:
    return (ensure_workspace_dir() / "apps").resolve()


def _validate_content(content: str) -> str | None:
    if not isinstance(content, str) or not content.strip():
        return "error: content is required"
    if len(content) > _MAX_QUERY_CHARS:
        return f"error: content exceeds {_MAX_QUERY_CHARS} characters"
    return None


def _keyword_candidates(content: str) -> list[str]:
    candidates: list[str] = []
    for raw in _TOKEN_RE.findall(content):
        lowered = raw.lower().strip("._:/@+-")
        if lowered:
            candidates.append(lowered)

        expanded = _CAMEL_BOUNDARY_RE.sub(" ", raw)
        for part in re.split(r"[._:/@+\-\s]+", expanded):
            part = part.lower()
            if part:
                candidates.append(part)

    counts = Counter(
        word
        for word in candidates
        if (len(word) >= 3 or word.isdigit() or word.startswith("app_"))
        and word not in _STOP_WORDS
    )
    ranked = sorted(counts, key=lambda word: (-counts[word], -len(word), word))
    return ranked[:_MAX_KEYWORDS]


def _is_sensitive_file(path: Path) -> bool:
    lowered = path.name.lower()
    if lowered in _SENSITIVE_FILE_NAMES:
        return True
    if lowered.startswith(".env.") and lowered != ".env.example":
        return True
    return path.suffix.lower() in _SENSITIVE_SUFFIXES


def _iter_candidate_files(
    app_path: Path,
    *,
    under: Path | None = None,
    include_generated: bool = False,
    include_hidden: bool = False,
) -> Iterator[Path]:
    search_root = under or app_path
    skipped_dirs = _NOISE_DIRS | (set() if include_generated else _GENERATED_DIRS)
    for root, dirs, names in os.walk(search_root):
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in skipped_dirs and (include_hidden or not name.startswith("."))
        )
        for name in sorted(names):
            path = Path(root) / name
            if (
                path.is_symlink()
                or (not include_hidden and name.startswith(".") and name not in _SAFE_HIDDEN_FILES)
                or _is_sensitive_file(path)
            ):
                continue
            yield path


def _iter_app_files(
    app_path: Path,
    *,
    under: Path | None = None,
    include_generated: bool = False,
) -> Iterator[Path]:
    scanned_bytes = 0
    for path in _iter_candidate_files(
        app_path,
        under=under,
        include_generated=include_generated,
    ):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > _MAX_FILE_BYTES or scanned_bytes + size > _MAX_SCAN_BYTES_PER_APP:
            continue
        scanned_bytes += size
        yield path


def _read_text(path: Path) -> str | None:
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_FILE_BYTES:
        return None
    if looks_binary(raw[:1024]):
        return None
    return raw.decode("utf-8", errors="replace")


def _app_identity(app_path: Path) -> tuple[str, str]:
    app_id = app_path.name
    name = app_path.name
    manifest = app_path / ".aios-app.json"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return app_id, name
    return str(data.get("app_id") or app_id), str(data.get("name") or name)


def _matching_keywords(value: str, keywords: list[str]) -> list[str]:
    lowered = value.lower()
    return [keyword for keyword in keywords if keyword in lowered]


def _resolve_app_path(app_path: str) -> tuple[Path | None, str | None]:
    if not isinstance(app_path, str) or not app_path.strip():
        return None, "error: app_path is required"

    apps_root = _apps_root()
    raw = Path(app_path).expanduser()
    if raw.is_absolute():
        unresolved = raw
    elif raw.parts and raw.parts[0] == "apps":
        unresolved = ensure_workspace_dir() / raw
    else:
        unresolved = apps_root / raw
    if unresolved.is_symlink():
        return None, f"error: symlink app paths are not allowed: {app_path}"
    candidate = unresolved.resolve()

    try:
        relative = candidate.relative_to(apps_root)
    except ValueError:
        return None, f"error: app_path must be inside {apps_root}"
    if not relative.parts:
        return None, "error: app_path must identify one app, not the apps directory"
    if not candidate.is_dir():
        return None, f"error: app path does not exist: {candidate}"
    return candidate, None


def _resolve_app_subpath(
    app_path: Path,
    relative_path: str,
    *,
    expected: str | None = None,
) -> tuple[Path | None, str | None]:
    if not isinstance(relative_path, str) or not relative_path.strip():
        return None, "error: path is required"
    raw = Path(relative_path).expanduser()
    unresolved = raw if raw.is_absolute() else app_path / raw
    if unresolved.is_symlink():
        return None, f"error: symlink paths are not allowed: {relative_path}"
    candidate = unresolved.resolve()
    try:
        candidate.relative_to(app_path)
    except ValueError:
        return None, f"error: path must remain inside app: {relative_path}"
    if not candidate.exists():
        return None, f"error: path does not exist in app: {relative_path}"
    if expected == "file" and not candidate.is_file():
        return None, f"error: path is not a file: {relative_path}"
    if expected == "directory" and not candidate.is_dir():
        return None, f"error: path is not a directory: {relative_path}"
    if candidate.is_file() and _is_sensitive_file(candidate):
        return None, f"error: sensitive files cannot be accessed: {relative_path}"
    return candidate, None


def _app_summary(app_path: Path) -> dict[str, str]:
    app_id, name = _app_identity(app_path)
    return {"app_id": app_id, "name": name, "path": str(app_path)}


def _json_result(payload: dict[str, object]) -> str:
    return truncate_middle(json.dumps(payload, indent=2, ensure_ascii=True))


def _normalize_extensions(extensions: list[str] | None) -> tuple[set[str] | None, str | None]:
    if extensions is None:
        return None, None
    if not isinstance(extensions, list) or not all(isinstance(item, str) for item in extensions):
        return None, "error: extensions must be an array of strings"
    normalized = {item.lower().lstrip(".").strip() for item in extensions if item.strip()}
    if any(not re.fullmatch(r"[a-z0-9][a-z0-9+_-]*", item) for item in normalized):
        return None, "error: extensions contain an invalid value"
    return normalized, None


def _normalize_terms(
    values: list[str] | None,
    argument_name: str,
) -> tuple[list[str] | None, str | None]:
    if values is None:
        return None, None
    if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
        return None, f"error: {argument_name} must be an array of strings"
    normalized = [item.strip().lower() for item in values if item.strip()]
    return normalized or None, None


def _matches_extension(path: Path, extensions: set[str] | None) -> bool:
    if extensions is None:
        return True
    return path.suffix.lower().lstrip(".") in extensions


def _selected_files(
    app_path: Path,
    *,
    paths: list[str] | None = None,
    under: str = ".",
    extensions: set[str] | None = None,
    include_generated: bool = False,
) -> tuple[list[Path] | None, str | None]:
    if paths is not None:
        if not isinstance(paths, list) or not paths or not all(isinstance(item, str) for item in paths):
            return None, "error: paths must be a non-empty array of strings"
        selected: list[Path] = []
        for relative_path in paths:
            candidate, error = _resolve_app_subpath(app_path, relative_path, expected="file")
            if error:
                return None, error
            assert candidate is not None
            if _matches_extension(candidate, extensions):
                selected.append(candidate)
        return selected, None

    search_root, error = _resolve_app_subpath(app_path, under, expected="directory")
    if error:
        return None, error
    assert search_root is not None
    return [
        path
        for path in _iter_candidate_files(
            app_path,
            under=search_root,
            include_generated=include_generated,
        )
        if _matches_extension(path, extensions)
    ], None


def find_relevant_apps(content: str, limit: int = 5):
    """Rank durable apps whose metadata, filenames, or source text match a text blob.

    Args:
        content: Natural-language text, keywords, logs, or code generated by the LLM.
        limit: Maximum apps to return (default 5, max 20).
    """
    error = _validate_content(content)
    if error:
        return error
    keywords = _keyword_candidates(content)
    if not keywords:
        return "error: content does not contain searchable keywords"
    limit = min(max(int(limit or 5), 1), 20)

    apps_root = _apps_root()
    if not apps_root.is_dir():
        return f"error: apps directory does not exist: {apps_root}"
    app_paths = sorted(
        path for path in apps_root.iterdir() if path.is_dir() and not path.is_symlink()
    )
    if not app_paths:
        return "none: no apps found"

    app_term_scores: dict[Path, Counter[str]] = defaultdict(Counter)
    app_evidence: dict[Path, list[dict[str, object]]] = defaultdict(list)
    identities: dict[Path, tuple[str, str]] = {}

    for app_path in app_paths:
        app_id, name = _app_identity(app_path)
        identities[app_path] = (app_id, name)
        identity_matches = _matching_keywords(f"{app_id} {name} {app_path.name}", keywords)
        for keyword in identity_matches:
            app_term_scores[app_path][keyword] += 12
        if identity_matches:
            app_evidence[app_path].append(
                {"source": "app identity", "matched_keywords": identity_matches}
            )

        for path in _iter_app_files(app_path):
            relative = path.relative_to(app_path)
            path_matches = _matching_keywords(str(relative), keywords)
            for keyword in path_matches:
                app_term_scores[app_path][keyword] += 4
            if path_matches and len(app_evidence[app_path]) < _MAX_EVIDENCE_PER_APP:
                app_evidence[app_path].append(
                    {"source": str(relative), "matched_keywords": path_matches, "kind": "path"}
                )

            text = _read_text(path)
            if text is None:
                continue
            lowered = text.lower()
            content_matches = [keyword for keyword in keywords if keyword in lowered]
            weight = 6 if path.name.lower() in _METADATA_FILES else 1
            for keyword in content_matches:
                app_term_scores[app_path][keyword] += weight * min(lowered.count(keyword), 5)
            if content_matches and len(app_evidence[app_path]) < _MAX_EVIDENCE_PER_APP:
                matching_line = next(
                    (
                        (number, line.strip())
                        for number, line in enumerate(text.splitlines(), start=1)
                        if any(keyword in line.lower() for keyword in content_matches)
                    ),
                    None,
                )
                evidence: dict[str, object] = {
                    "source": str(relative),
                    "matched_keywords": content_matches,
                    "kind": "content",
                }
                if matching_line:
                    evidence["line"] = matching_line[0]
                    evidence["text"] = matching_line[1][:240]
                app_evidence[app_path].append(evidence)

    document_frequency = Counter(
        keyword
        for scores in app_term_scores.values()
        for keyword, score in scores.items()
        if score > 0
    )
    ranked: list[tuple[float, Path]] = []
    for app_path, term_scores in app_term_scores.items():
        score = sum(
            term_score * (1.0 + math.log((len(app_paths) + 1) / (document_frequency[keyword] + 1)))
            for keyword, term_score in term_scores.items()
        )
        if score > 0:
            ranked.append((score, app_path))
    ranked.sort(key=lambda item: (-item[0], identities[item[1]][1].lower(), str(item[1])))

    results = []
    for score, app_path in ranked[:limit]:
        app_id, name = identities[app_path]
        results.append(
            {
                "app_id": app_id,
                "name": name,
                "path": str(app_path),
                "score": round(score, 2),
                "matched_keywords": sorted(app_term_scores[app_path]),
                "evidence": app_evidence[app_path],
            }
        )

    if not results:
        return "none: no apps matched the supplied content"
    return truncate_middle(
        json.dumps({"query_keywords": keywords, "apps": results}, indent=2, ensure_ascii=True)
    )


def inspect_app(
    app_path: str,
    max_depth: int = 3,
    include_hidden: bool = False,
    limit: int = 300,
):
    """Summarize an app's identity, components, important files, and directory tree."""
    resolved_app_path, error = _resolve_app_path(app_path)
    if error:
        return error
    assert resolved_app_path is not None
    max_depth = min(max(int(max_depth or 3), 1), 10)
    limit = min(max(int(limit or 300), 1), 1_000)

    tree: list[dict[str, object]] = []
    extension_counts: Counter[str] = Counter()
    important_files: list[str] = []
    components: list[str] = []
    deploy_manifest = resolved_app_path / "aios.deploy.yaml"
    if not deploy_manifest.exists():
        deploy_manifest = resolved_app_path / "aios.deploy.yml"
    deploy_text = _read_text(deploy_manifest) if deploy_manifest.exists() else None
    if deploy_text:
        components = [
            component
            for component in ("database", "server", "frontend")
            if re.search(rf"(?m)^{component}:\s*(?:#.*)?$", deploy_text)
        ]

    skipped_dirs = _NOISE_DIRS | _GENERATED_DIRS
    total_files = 0
    total_directories = 0
    for root, dirs, names in os.walk(resolved_app_path):
        root_path = Path(root)
        depth = len(root_path.relative_to(resolved_app_path).parts)
        dirs[:] = sorted(
            name
            for name in dirs
            if name not in skipped_dirs and (include_hidden or not name.startswith("."))
        )
        if depth >= max_depth:
            dirs[:] = []
        for name in dirs:
            total_directories += 1
            if len(tree) < limit:
                relative = (root_path / name).relative_to(resolved_app_path)
                tree.append({"path": str(relative), "type": "directory", "depth": depth + 1})
        for name in sorted(names):
            path = root_path / name
            if (
                path.is_symlink()
                or _is_sensitive_file(path)
                or (not include_hidden and name.startswith(".") and name not in _SAFE_HIDDEN_FILES)
            ):
                continue
            total_files += 1
            extension_counts[path.suffix.lower().lstrip(".") or "(none)"] += 1
            relative = path.relative_to(resolved_app_path)
            if name.lower() in _METADATA_FILES:
                important_files.append(str(relative))
            if depth <= max_depth and len(tree) < limit:
                try:
                    size = path.stat().st_size
                except OSError:
                    size = None
                tree.append(
                    {"path": str(relative), "type": "file", "depth": depth + 1, "size": size}
                )

    return _json_result(
        {
            "app": _app_summary(resolved_app_path),
            "components": components,
            "important_files": sorted(important_files),
            "summary": {
                "files": total_files,
                "directories": total_directories,
                "extensions": dict(extension_counts.most_common(30)),
            },
            "tree": tree,
            "tree_truncated": total_files + total_directories > len(tree),
            "max_depth": max_depth,
        }
    )


def list_app_files(
    app_path: str,
    under: str = ".",
    extensions: list[str] | None = None,
    name_contains: list[str] | None = None,
    path_contains: list[str] | None = None,
    include_generated: bool = False,
    limit: int = 200,
):
    """Inventory files in one app using structured path, name, and extension filters."""
    resolved_app_path, error = _resolve_app_path(app_path)
    if error:
        return error
    assert resolved_app_path is not None
    normalized_extensions, error = _normalize_extensions(extensions)
    if error:
        return error
    normalized_names, error = _normalize_terms(name_contains, "name_contains")
    if error:
        return error
    normalized_paths, error = _normalize_terms(path_contains, "path_contains")
    if error:
        return error
    limit = min(max(int(limit or 200), 1), 1_000)

    search_root, error = _resolve_app_subpath(resolved_app_path, under, expected="directory")
    if error:
        return error
    assert search_root is not None
    matches: list[dict[str, object]] = []
    total_matches = 0
    for path in _iter_candidate_files(
        resolved_app_path,
        under=search_root,
        include_generated=bool(include_generated),
    ):
        relative = str(path.relative_to(resolved_app_path))
        if not _matches_extension(path, normalized_extensions):
            continue
        if normalized_names and not any(term in path.name.lower() for term in normalized_names):
            continue
        if normalized_paths and not any(term in relative.lower() for term in normalized_paths):
            continue
        total_matches += 1
        if len(matches) >= limit:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        matches.append(
            {
                "path": relative,
                "name": path.name,
                "extension": path.suffix.lower().lstrip("."),
                "size": size,
            }
        )

    matches.sort(key=lambda item: str(item["path"]))

    return _json_result(
        {
            "app": _app_summary(resolved_app_path),
            "under": str(search_root.relative_to(resolved_app_path)) or ".",
            "files": matches,
            "total_matches": total_matches,
            "truncated": total_matches > len(matches),
        }
    )


def _compile_content_matcher(
    query: str,
    match_mode: str,
) -> tuple[object | None, list[str] | None, str | None]:
    error = _validate_content(query)
    if error:
        return None, None, error.replace("content", "query", 1)
    match_mode = str(match_mode or "keywords").lower()
    if match_mode == "keywords":
        keywords = _keyword_candidates(query)
        if not keywords:
            return None, None, "error: query does not contain searchable keywords"
        return None, keywords, None
    if match_mode == "literal":
        return query.lower(), [query], None
    if match_mode == "regex":
        if len(query) > 500:
            return None, None, "error: regex query exceeds 500 characters"
        try:
            return re.compile(query, re.IGNORECASE), [query], None
        except re.error as exc:
            return None, None, f"error: invalid regex: {exc}"
    return None, None, "error: match_mode must be keywords, literal, or regex"


def search_app_content(
    app_path: str,
    query: str,
    paths: list[str] | None = None,
    under: str = ".",
    extensions: list[str] | None = None,
    match_mode: str = "keywords",
    context: int = 1,
    include_generated: bool = False,
    limit: int = 50,
):
    """Search text inside selected files or a directory scope within one app."""
    resolved_app_path, error = _resolve_app_path(app_path)
    if error:
        return error
    assert resolved_app_path is not None
    normalized_extensions, error = _normalize_extensions(extensions)
    if error:
        return error
    matcher, query_terms, error = _compile_content_matcher(query, match_mode)
    if error:
        return error
    assert query_terms is not None
    selected_files, error = _selected_files(
        resolved_app_path,
        paths=paths,
        under=under,
        extensions=normalized_extensions,
        include_generated=bool(include_generated),
    )
    if error:
        return error
    assert selected_files is not None
    context = min(max(int(context or 0), 0), 10)
    limit = min(max(int(limit or 50), 1), 200)
    mode = str(match_mode or "keywords").lower()

    matches: list[dict[str, object]] = []
    for path in selected_files:
        text = _read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if mode == "keywords":
                matched_terms = _matching_keywords(line, query_terms)
            elif mode == "literal":
                matched_terms = query_terms if str(matcher) in line.lower() else []
            else:
                assert isinstance(matcher, re.Pattern)
                matched_terms = query_terms if matcher.search(line) else []
            if not matched_terms:
                continue
            score = len(matched_terms) * 5
            if path.name.lower() in _METADATA_FILES:
                score += 3
            matches.append(
                {
                    "path": str(path.relative_to(resolved_app_path)),
                    "line": index + 1,
                    "text": line.strip()[:_MAX_LINE_CHARS],
                    "before": [
                        value[:_MAX_LINE_CHARS] for value in lines[max(0, index - context):index]
                    ],
                    "after": [
                        value[:_MAX_LINE_CHARS] for value in lines[index + 1:index + context + 1]
                    ],
                    "matched_terms": matched_terms,
                    "score": score,
                }
            )

    matches.sort(
        key=lambda match: (-int(match["score"]), str(match["path"]), int(match["line"]))
    )
    selected_matches = matches[:limit]
    return _json_result(
        {
            "app": _app_summary(resolved_app_path),
            "query": query,
            "match_mode": mode,
            "searched_files": len(selected_files),
            "matches": selected_matches,
            "total_matches": len(matches),
            "truncated": len(matches) > len(selected_matches),
        }
    )


def find_app_references(
    app_path: str,
    targets: list[str],
    under: str = ".",
    extensions: list[str] | None = None,
    context: int = 1,
    include_generated: bool = False,
    limit: int = 100,
):
    """Find literal references to asset names, module names, symbols, or paths in an app."""
    normalized_targets, error = _normalize_terms(targets, "targets")
    if error:
        return error
    if not normalized_targets:
        return "error: targets must contain at least one non-empty value"
    if any(len(target) > 500 for target in normalized_targets):
        return "error: each target must be at most 500 characters"
    resolved_app_path, error = _resolve_app_path(app_path)
    if error:
        return error
    assert resolved_app_path is not None
    normalized_extensions, error = _normalize_extensions(extensions)
    if error:
        return error
    selected_files, error = _selected_files(
        resolved_app_path,
        under=under,
        extensions=normalized_extensions,
        include_generated=bool(include_generated),
    )
    if error:
        return error
    assert selected_files is not None
    context = min(max(int(context or 0), 0), 10)
    limit = min(max(int(limit or 100), 1), 200)

    matches: list[dict[str, object]] = []
    for path in selected_files:
        text = _read_text(path)
        if text is None:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            lowered = line.lower()
            matched_targets = [target for target in normalized_targets if target in lowered]
            if not matched_targets:
                continue
            matches.append(
                {
                    "path": str(path.relative_to(resolved_app_path)),
                    "line": index + 1,
                    "text": line.strip()[:_MAX_LINE_CHARS],
                    "before": [
                        value[:_MAX_LINE_CHARS] for value in lines[max(0, index - context):index]
                    ],
                    "after": [
                        value[:_MAX_LINE_CHARS] for value in lines[index + 1:index + context + 1]
                    ],
                    "matched_targets": matched_targets,
                }
            )

    matches.sort(key=lambda match: (str(match["path"]), int(match["line"])))
    selected_matches = matches[:limit]
    return _json_result(
        {
            "app": _app_summary(resolved_app_path),
            "targets": normalized_targets,
            "searched_files": len(selected_files),
            "matches": selected_matches,
            "total_matches": len(matches),
            "truncated": len(matches) > len(selected_matches),
        }
    )


def read_app_file(
    app_path: str,
    file_path: str,
    offset: int = 0,
    limit: int = 200,
):
    """Read a paginated text file while enforcing the selected app boundary."""
    resolved_app_path, error = _resolve_app_path(app_path)
    if error:
        return error
    assert resolved_app_path is not None
    resolved_file, error = _resolve_app_subpath(resolved_app_path, file_path, expected="file")
    if error:
        return error
    assert resolved_file is not None
    text = _read_text(resolved_file)
    if text is None:
        return f"error: file is binary or unreadable: {file_path}"
    offset = max(int(offset or 0), 0)
    limit = min(max(int(limit or 200), 1), _MAX_READ_LINES)
    lines = text.splitlines()
    page = lines[offset:offset + limit]
    return _json_result(
        {
            "app": _app_summary(resolved_app_path),
            "path": str(resolved_file.relative_to(resolved_app_path)),
            "offset": offset,
            "lines": [
                {"number": offset + index + 1, "text": line[:_MAX_LINE_CHARS]}
                for index, line in enumerate(page)
            ],
            "total_lines": len(lines),
            "truncated": offset + len(page) < len(lines),
            "next_offset": offset + len(page) if offset + len(page) < len(lines) else None,
        }
    )


def search_app(app_path: str, content: str, limit: int = 50):
    """Backward-compatible wrapper for keyword-based app content search."""
    result = search_app_content(
        app_path=app_path,
        query=content,
        match_mode="keywords",
        context=0,
        limit=limit,
    )
    if result.startswith("error:"):
        return result
    data = json.loads(result)
    if not data["matches"]:
        return "none: no matches found in app"
    data["query_keywords"] = _keyword_candidates(content)
    data.pop("query", None)
    data.pop("match_mode", None)
    data.pop("searched_files", None)
    for match in data["matches"]:
        match["matched_keywords"] = match.pop("matched_terms")
    return _json_result(data)
