from __future__ import annotations

from ..apps import create_app, get_app, list_apps, read_app_state, set_app_status


def app(
    action: str,
    app_id: str = None,
    slug: str = None,
    name: str = None,
    description: str = None,
    entrypoint_content: str = None,
    run_on_startup: bool = None,
    app_type: str = None,
    port: int = None,
    start_command: str = None,
):
    if action == "create":
        if not name or not description:
            return "error: create requires name and description"
        try:
            return create_app(
                name=name,
                description=description,
                entrypoint_content=entrypoint_content,
                slug=slug,
                run_on_startup=bool(run_on_startup),
                app_type=app_type or "static",
                port=port,
                start_command=start_command,
            )
        except FileExistsError:
            return "error: app folder already exists"
        except ValueError as exc:
            return f"error: {exc}"

    if action == "list":
        return {"apps": list_apps()}

    if action == "get":
        try:
            app_record = get_app(app_id=app_id, slug=slug)
        except ValueError as exc:
            return f"error: {exc}"
        if app_record is None:
            return "error: app not found"
        return app_record

    if action == "activate":
        try:
            return set_app_status(app_id=app_id, slug=slug, status="active")
        except ValueError as exc:
            return f"error: {exc}"

    if action == "pause":
        try:
            return set_app_status(app_id=app_id, slug=slug, status="paused")
        except ValueError as exc:
            return f"error: {exc}"

    if action == "state_get":
        try:
            return read_app_state(app_id=app_id, slug=slug)
        except ValueError as exc:
            return f"error: {exc}"

    return (
        f"error: unknown action '{action}'. "
        "Use create, list, get, activate, pause, or state_get."
    )
