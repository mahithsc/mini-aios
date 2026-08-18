"""Trusted one-shot bridge from Pi to the AIOS cloud control plane.

The bridge deliberately exposes a finite command protocol instead of a generic
HTTP or provider client. Deployments are rooted at the nearest
``aios.deploy.yaml`` containing Pi's working directory, and media uploads are
confined to that same app workspace. Database access is structured and
read-only; no arbitrary SQL, provider credentials, or source-directory
arguments cross the boundary.

Exactly one JSON object is written to stdout for every request.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

# The extension invokes this file by absolute path from an app workspace.
if __package__ in {None, ""}:  # pragma: no cover - exercised by subprocess test
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from aios_core.app_workspaces import APP_METADATA_NAME
from aios_core.deploy.cloud_client import (
    CloudDeployClient,
    CloudDeployError,
    DeploymentComponent,
)
from aios_core.deploy.manifest import (
    ManifestValidationError,
    find_deployment_root,
    load_deployment_manifest,
)

BRIDGE_VERSION = 3
_COMPONENT_ORDER: tuple[DeploymentComponent, ...] = (
    "database",
    "server",
    "frontend",
)
_RESOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_APP_ID_RE = re.compile(r"^app_[A-Za-z0-9]+$")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,191}$")
_DATABASE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,127}$")
_QUERY_OPERATOR_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_CONTENT_TYPE_RE = re.compile(r"^(?:image|video|audio)/[A-Za-z0-9.+-]{1,100}$")
_ClientFactory = Callable[[], CloudDeployClient]


class BridgeRequestError(ValueError):
    """The trusted extension sent a request that the bridge will not execute."""


def normalize_components(
    values: Sequence[str] | None,
) -> list[DeploymentComponent] | None:
    """Validate, deduplicate, and order explicitly requested deployment tiers."""

    if values is None:
        return None
    if not values:
        raise BridgeRequestError("components must contain at least one deployment tier")
    requested = set(values)
    invalid = sorted(requested.difference(_COMPONENT_ORDER))
    if invalid:
        raise BridgeRequestError(
            "unknown deployment component(s): " + ", ".join(invalid)
        )
    return [component for component in _COMPONENT_ORDER if component in requested]


def _validate_pattern(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise BridgeRequestError(f"{label} has an invalid format")
    return value


def validate_resource_id(value: str, label: str) -> str:
    return _validate_pattern(value, _RESOURCE_ID_RE, label)


def validate_app_id(value: str) -> str:
    return _validate_pattern(value, _APP_ID_RE, "app_id")


def validate_operation_id(value: str) -> str:
    return _validate_pattern(value, _OPERATION_ID_RE, "operation_id")


def validate_database_name(value: str, label: str) -> str:
    safe = _validate_pattern(value, _DATABASE_NAME_RE, label)
    if ".." in safe:
        raise BridgeRequestError(f"{label} has an invalid format")
    return safe


def _validate_int(value: str, label: str, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise BridgeRequestError(f"{label} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise BridgeRequestError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _validate_destination(value: str | None) -> str | None:
    if value is None:
        return None
    if not value or len(value) > 512 or "\\" in value:
        raise BridgeRequestError("destination must be a safe relative object path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BridgeRequestError("destination must be a safe relative object path")
    if any(ord(character) < 32 for character in value):
        raise BridgeRequestError("destination must be a safe relative object path")
    return value


def _validate_content_type(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not _CONTENT_TYPE_RE.fullmatch(normalized):
        raise BridgeRequestError(
            "content_type must be an image, video, or audio MIME type"
        )
    return normalized


def _app_root_from_cwd(cwd: Path | None = None) -> Path:
    try:
        source_dir = (cwd if cwd is not None else Path.cwd()).resolve(strict=True)
    except OSError as exc:
        raise BridgeRequestError("the Pi working directory does not exist") from exc
    if not source_dir.is_dir():
        raise BridgeRequestError("the Pi working directory is not a directory")
    app_dir = find_deployment_root(source_dir)
    if app_dir is None:
        raise BridgeRequestError(
            "aios.deploy.yaml was not found in the current directory or its parents"
        )
    return app_dir.resolve()


def _validated_workspace_manifest(app_dir: Path):
    """Require Pi deployments to retain the durable workspace's cloud identity."""

    manifest = load_deployment_manifest(app_dir)
    metadata_path = app_dir / APP_METADATA_NAME
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BridgeRequestError(
            f"{APP_METADATA_NAME} is missing or invalid; resolve this app's durable workspace first"
        ) from exc
    if not isinstance(metadata, dict) or metadata.get("app_id") != manifest.app_id:
        raise BridgeRequestError(
            f"{APP_METADATA_NAME} app_id must match aios.deploy.yaml"
        )
    if _APP_ID_RE.fullmatch(app_dir.name) and app_dir.name != manifest.app_id:
        raise BridgeRequestError(
            "workspace directory name must match aios.deploy.yaml app_id"
        )
    return manifest


def _client(factory: _ClientFactory | None) -> CloudDeployClient:
    return (factory or CloudDeployClient)()


def _cloud_payload(result: Any, **context: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError("cloud deployment client returned a non-object response")
    payload = dict(result)
    status = payload.get("status")
    if status is None:
        payload["status"] = "ok"
    elif not isinstance(status, str):
        raise TypeError("cloud deployment client returned an invalid status")
    payload.update(context)
    payload["bridge_version"] = BRIDGE_VERSION
    return payload


def deploy_from_cwd(
    *,
    operation_id: str,
    components: Sequence[str] | None = None,
    _cwd: Path | None = None,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    """Package the manifest-rooted app and enqueue one idempotent pipeline."""

    app_dir = _app_root_from_cwd(_cwd)
    _validated_workspace_manifest(app_dir)
    normalized = normalize_components(components)
    safe_operation_id = validate_operation_id(operation_id)
    result = _client(_client_factory).deploy_pipeline(
        app_dir,
        components=normalized,
        idempotency_key=safe_operation_id,
    )
    return _cloud_payload(result, source_dir=str(app_dir))


def get_pipeline_status(
    pipeline_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_resource_id(pipeline_id, "pipeline_id")
    result = _client(_client_factory).get_deployment_pipeline(safe_id)
    return _cloud_payload(result, pipeline_id=safe_id)


def get_deployment_status(
    deployment_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_resource_id(deployment_id, "deployment_id")
    return _cloud_payload(
        _client(_client_factory).get_deployment(safe_id),
        deployment_id=safe_id,
    )


def get_deployment_events(
    deployment_id: str,
    *,
    after: int = -1,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_resource_id(deployment_id, "deployment_id")
    if not -1 <= after <= 2_147_483_647:
        raise BridgeRequestError("after must be between -1 and 2147483647")
    return _cloud_payload(
        _client(_client_factory).get_deployment_events(safe_id, after=after),
        deployment_id=safe_id,
    )


def get_app_info(
    app_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_app_id(app_id)
    return _cloud_payload(
        _client(_client_factory).get_app_info(safe_id), app_id=safe_id
    )


def check_app_status(
    app_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_app_id(app_id)
    return _cloud_payload(
        _client(_client_factory).check_app_status(safe_id),
        app_id=safe_id,
    )


def _deployment_control(
    action: str,
    deployment_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_resource_id(deployment_id, "deployment_id")
    client = _client(_client_factory)
    operations = {
        "cancel": client.cancel_deployment,
        "resume": client.resume_deployment,
        "rollback": client.rollback_deployment,
    }
    try:
        operation = operations[action]
    except KeyError as exc:  # pragma: no cover - internal invariant
        raise RuntimeError(f"unsupported deployment control action: {action}") from exc
    return _cloud_payload(operation(safe_id), deployment_id=safe_id)


def cancel_deployment(
    deployment_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    return _deployment_control("cancel", deployment_id, _client_factory=_client_factory)


def resume_deployment(
    deployment_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    return _deployment_control("resume", deployment_id, _client_factory=_client_factory)


def rollback_deployment(
    deployment_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    return _deployment_control(
        "rollback", deployment_id, _client_factory=_client_factory
    )


def upload_app_media(
    app_id: str,
    local_path: str,
    *,
    destination: str | None = None,
    content_type: str | None = None,
    _cwd: Path | None = None,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    """Upload media only from the manifest-rooted current app workspace."""

    safe_app_id = validate_app_id(app_id)
    app_dir = _app_root_from_cwd(_cwd)
    manifest = _validated_workspace_manifest(app_dir)
    if manifest.app_id != safe_app_id:
        raise BridgeRequestError(
            "app_id must match the current workspace's aios.deploy.yaml"
        )
    if not isinstance(local_path, str) or not local_path.strip():
        raise BridgeRequestError("local_path cannot be empty")
    working_dir = (_cwd if _cwd is not None else Path.cwd()).resolve(strict=True)
    candidate = Path(local_path).expanduser()
    if not candidate.is_absolute():
        candidate = working_dir / candidate
    try:
        safe_local_path = candidate.resolve(strict=True)
    except OSError as exc:
        raise BridgeRequestError(
            "local_path must reference an existing media file"
        ) from exc
    if not safe_local_path.is_file() or not safe_local_path.is_relative_to(app_dir):
        raise BridgeRequestError(
            "local_path must be a file inside the current app workspace"
        )
    safe_destination = _validate_destination(destination)
    safe_content_type = _validate_content_type(content_type)
    result = _client(_client_factory).upload_app_media(
        safe_app_id,
        str(safe_local_path),
        destination=safe_destination,
        content_type=safe_content_type,
        allowed_root=app_dir,
    )
    return _cloud_payload(result, app_id=safe_app_id)


def list_app_media(
    app_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_app_id(app_id)
    return _cloud_payload(
        _client(_client_factory).list_app_media(safe_id), app_id=safe_id
    )


def get_app_media_url(
    app_id: str,
    media_id: str,
    *,
    expires_in: int = 3600,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_app_id = validate_app_id(app_id)
    safe_media_id = validate_resource_id(media_id, "media_id")
    if not 60 <= expires_in <= 86_400:
        raise BridgeRequestError("expires_in must be between 60 and 86400")
    return _cloud_payload(
        _client(_client_factory).get_app_media_url(
            safe_app_id,
            safe_media_id,
            expires_in=expires_in,
        ),
        app_id=safe_app_id,
        media_id=safe_media_id,
    )


def delete_app_media(
    app_id: str,
    media_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_app_id = validate_app_id(app_id)
    safe_media_id = validate_resource_id(media_id, "media_id")
    return _cloud_payload(
        _client(_client_factory).delete_app_media(safe_app_id, safe_media_id),
        app_id=safe_app_id,
        media_id=safe_media_id,
    )


def list_database_tables(
    app_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_app_id(app_id)
    return _cloud_payload(
        _client(_client_factory).list_database_tables(safe_id),
        app_id=safe_id,
    )


def inspect_database_table(
    app_id: str,
    table: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_app_id = validate_app_id(app_id)
    safe_table = validate_database_name(table, "table")
    return _cloud_payload(
        _client(_client_factory).inspect_database_table(safe_app_id, safe_table),
        app_id=safe_app_id,
        table=safe_table,
    )


def _normalize_columns(columns: Any) -> list[str] | None:
    if columns is None:
        return None
    if not isinstance(columns, list) or len(columns) > 100:
        raise BridgeRequestError("columns must be an array with at most 100 items")
    return [validate_database_name(column, "column") for column in columns]


def _normalize_filters(filters: Any) -> list[dict[str, Any]] | None:
    if filters is None:
        return None
    if not isinstance(filters, list) or len(filters) > 50:
        raise BridgeRequestError("filters must be an array with at most 50 items")
    normalized: list[dict[str, Any]] = []
    for item in filters:
        if not isinstance(item, dict) or set(item).difference(
            {"column", "op", "value"}
        ):
            raise BridgeRequestError(
                "each filter must contain only column, op, and optional value"
            )
        column = validate_database_name(item.get("column"), "filter column")
        op = _validate_pattern(item.get("op"), _QUERY_OPERATOR_RE, "filter op")
        normalized.append(
            {
                "column": column,
                "op": op,
                **({"value": item["value"]} if "value" in item else {}),
            }
        )
    return normalized


def _normalize_order(order: Any) -> list[dict[str, Any]] | None:
    if order is None:
        return None
    if not isinstance(order, list) or len(order) > 20:
        raise BridgeRequestError("order must be an array with at most 20 items")
    normalized: list[dict[str, Any]] = []
    for item in order:
        if not isinstance(item, dict) or set(item) != {"column", "direction"}:
            raise BridgeRequestError(
                "each order item must contain exactly column and direction"
            )
        direction = item["direction"]
        if direction not in {"asc", "desc"}:
            raise BridgeRequestError("order direction must be asc or desc")
        normalized.append(
            {
                "column": validate_database_name(item["column"], "order column"),
                "direction": direction,
            }
        )
    return normalized


def query_database_table(
    app_id: str,
    table: str,
    *,
    columns: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    order: list[dict[str, Any]] | None = None,
    limit: int = 100,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    """Run a policy-checked structured read without accepting SQL text."""

    safe_app_id = validate_app_id(app_id)
    safe_table = validate_database_name(table, "table")
    if not 1 <= limit <= 1_000:
        raise BridgeRequestError("limit must be between 1 and 1000")
    result = _client(_client_factory).query_database_table(
        safe_app_id,
        safe_table,
        columns=_normalize_columns(columns),
        filters=_normalize_filters(filters),
        order=_normalize_order(order),
        limit=limit,
    )
    return _cloud_payload(result, app_id=safe_app_id, table=safe_table)


def list_database_migrations(
    app_id: str,
    *,
    _client_factory: _ClientFactory | None = None,
) -> dict[str, Any]:
    safe_id = validate_app_id(app_id)
    return _cloud_payload(
        _client(_client_factory).list_database_migrations(safe_id),
        app_id=safe_id,
    )


def _parse_options(
    values: Sequence[str],
    *,
    allowed: set[str],
    repeatable: set[str] | None = None,
) -> dict[str, str | list[str]]:
    repeated = repeatable or set()
    options: dict[str, str | list[str]] = {}
    index = 0
    while index < len(values):
        flag = values[index]
        if flag not in allowed or index + 1 >= len(values):
            raise BridgeRequestError(f"unsupported or incomplete option: {flag}")
        value = values[index + 1]
        if flag in repeated:
            current = options.setdefault(flag, [])
            assert isinstance(current, list)
            current.append(value)
        elif flag in options:
            raise BridgeRequestError(f"option may be supplied only once: {flag}")
        else:
            options[flag] = value
        index += 2
    return options


def _required(options: dict[str, str | list[str]], flag: str) -> str:
    value = options.get(flag)
    if not isinstance(value, str):
        raise BridgeRequestError(f"missing required option: {flag}")
    return value


def _optional(options: dict[str, str | list[str]], flag: str) -> str | None:
    value = options.get(flag)
    if value is not None and not isinstance(value, str):  # pragma: no cover
        raise BridgeRequestError(f"invalid option: {flag}")
    return value


def _parse_json_array(value: str | None, label: str) -> list[Any] | None:
    if value is None:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BridgeRequestError(f"{label} must be a valid JSON array") from exc
    if not isinstance(parsed, list):
        raise BridgeRequestError(f"{label} must be a valid JSON array")
    return parsed


def _single_id_request(
    values: Sequence[str],
    *,
    flag: str,
    field: str,
    validator: Callable[[str], str],
) -> dict[str, Any]:
    options = _parse_options(values, allowed={flag})
    return {field: validator(_required(options, flag))}


def _parse_request(argv: Sequence[str]) -> tuple[str, dict[str, Any]]:
    if not argv:
        raise BridgeRequestError("a cloud bridge action is required")
    action, values = argv[0], argv[1:]

    if action == "deploy":
        options = _parse_options(
            values,
            allowed={"--component", "--operation-id"},
            repeatable={"--component"},
        )
        components = options.get("--component")
        return action, {
            "operation_id": validate_operation_id(_required(options, "--operation-id")),
            "components": normalize_components(
                components if isinstance(components, list) else None
            ),
        }
    if action == "status":
        return action, _single_id_request(
            values,
            flag="--pipeline-id",
            field="pipeline_id",
            validator=lambda value: validate_resource_id(value, "pipeline_id"),
        )
    if action in {
        "get-deployment",
        "cancel-deployment",
        "resume-deployment",
        "rollback-deployment",
    }:
        return action, _single_id_request(
            values,
            flag="--deployment-id",
            field="deployment_id",
            validator=lambda value: validate_resource_id(value, "deployment_id"),
        )
    if action == "get-deployment-events":
        options = _parse_options(
            values,
            allowed={"--deployment-id", "--after"},
        )
        after = _optional(options, "--after")
        return action, {
            "deployment_id": validate_resource_id(
                _required(options, "--deployment-id"), "deployment_id"
            ),
            "after": -1
            if after is None
            else _validate_int(after, "after", minimum=-1, maximum=2_147_483_647),
        }
    if action in {
        "get-app-info",
        "check-app-status",
        "list-media",
        "list-database-tables",
        "list-database-migrations",
    }:
        return action, _single_id_request(
            values,
            flag="--app-id",
            field="app_id",
            validator=validate_app_id,
        )
    if action == "upload-media":
        options = _parse_options(
            values,
            allowed={
                "--app-id",
                "--local-path",
                "--destination",
                "--content-type",
            },
        )
        return action, {
            "app_id": validate_app_id(_required(options, "--app-id")),
            "local_path": _required(options, "--local-path"),
            "destination": _validate_destination(_optional(options, "--destination")),
            "content_type": _validate_content_type(
                _optional(options, "--content-type")
            ),
        }
    if action in {"get-media-url", "delete-media"}:
        allowed = {"--app-id", "--media-id"}
        if action == "get-media-url":
            allowed.add("--expires-in")
        options = _parse_options(values, allowed=allowed)
        request: dict[str, Any] = {
            "app_id": validate_app_id(_required(options, "--app-id")),
            "media_id": validate_resource_id(
                _required(options, "--media-id"), "media_id"
            ),
        }
        expires_in = _optional(options, "--expires-in")
        if action == "get-media-url":
            request["expires_in"] = (
                3600
                if expires_in is None
                else _validate_int(expires_in, "expires_in", minimum=60, maximum=86_400)
            )
        return action, request
    if action == "inspect-database-table":
        options = _parse_options(values, allowed={"--app-id", "--table"})
        return action, {
            "app_id": validate_app_id(_required(options, "--app-id")),
            "table": validate_database_name(_required(options, "--table"), "table"),
        }
    if action == "query-database-table":
        options = _parse_options(
            values,
            allowed={
                "--app-id",
                "--table",
                "--columns-json",
                "--filters-json",
                "--order-json",
                "--limit",
            },
        )
        limit = _optional(options, "--limit")
        return action, {
            "app_id": validate_app_id(_required(options, "--app-id")),
            "table": validate_database_name(_required(options, "--table"), "table"),
            "columns": _normalize_columns(
                _parse_json_array(_optional(options, "--columns-json"), "columns")
            ),
            "filters": _normalize_filters(
                _parse_json_array(_optional(options, "--filters-json"), "filters")
            ),
            "order": _normalize_order(
                _parse_json_array(_optional(options, "--order-json"), "order")
            ),
            "limit": 100
            if limit is None
            else _validate_int(limit, "limit", minimum=1, maximum=1_000),
        }
    raise BridgeRequestError(f"unknown cloud bridge action: {action}")


def _error_payload(code: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "error_code": code,
        "error": message,
        "bridge_version": BRIDGE_VERSION,
    }


def _write_payload(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":"), default=str) + "\n")
    sys.stdout.flush()


def _dispatch(action: str, request: dict[str, Any]) -> dict[str, Any]:
    operations: dict[str, Callable[..., dict[str, Any]]] = {
        "deploy": deploy_from_cwd,
        "status": get_pipeline_status,
        "get-deployment": get_deployment_status,
        "get-deployment-events": get_deployment_events,
        "get-app-info": get_app_info,
        "check-app-status": check_app_status,
        "cancel-deployment": cancel_deployment,
        "resume-deployment": resume_deployment,
        "rollback-deployment": rollback_deployment,
        "upload-media": upload_app_media,
        "list-media": list_app_media,
        "get-media-url": get_app_media_url,
        "delete-media": delete_app_media,
        "list-database-tables": list_database_tables,
        "inspect-database-table": inspect_database_table,
        "query-database-table": query_database_table,
        "list-database-migrations": list_database_migrations,
    }
    return operations[action](**request)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        action, request = _parse_request(args)
    except BridgeRequestError as exc:
        _write_payload(_error_payload("invalid_request", str(exc)))
        return 2

    try:
        result = _dispatch(action, request)
    except BridgeRequestError as exc:
        _write_payload(_error_payload("invalid_request", str(exc)))
        return 2
    except (CloudDeployError, ManifestValidationError, OSError) as exc:
        _write_payload(_error_payload("cloud_error", str(exc)))
        return 0
    except Exception as exc:  # noqa: BLE001 - bridge must return structured diagnostics
        message = str(exc).strip() or type(exc).__name__
        _write_payload(
            _error_payload("bridge_failure", f"cloud bridge failed: {message}")
        )
        return 1

    _write_payload(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
