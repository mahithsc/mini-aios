def _get_cron_manager():
    # Imported lazily to avoid package import cycles.
    from ..crons import cron_manager

    return cron_manager


def cron(
    action: str,
    name: str = None,
    description: str = None,
    instructions: str = None,
    schedule: str = None,
    cron_id: str = None,
):
    cron_manager = _get_cron_manager()
    if action == "create":
        if not all([name, description, instructions, schedule]):
            return "error: create requires name, description, instructions, and schedule"
        try:
            cid = cron_manager.create_cron(name, description, instructions, schedule)
            return f"Cron created: {cid[:8]} ({name})"
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "list":
        return cron_manager.list_crons()

    elif action == "edit":
        if not cron_id:
            return "error: edit requires cron_id"
        try:
            return cron_manager.edit_cron(
                cron_id,
                name=name,
                description=description,
                instructions=instructions,
                schedule=schedule,
            )
        except ValueError as e:
            return f"error: invalid schedule -- {e}"

    elif action == "delete":
        if not cron_id:
            return "error: delete requires cron_id"
        return cron_manager.delete_cron(cron_id)

    else:
        return f"error: unknown action '{action}'. Use create, list, edit, or delete."
