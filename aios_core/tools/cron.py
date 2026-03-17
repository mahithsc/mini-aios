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
    timezone_name: str = None,
    run_at_utc: str = None,
    cron_id: str = None,
):
    cron_manager = _get_cron_manager()
    if action == "create":
        if not all([name, description, instructions]):
            return "error: create requires name, description, and instructions"
        try:
            cid = cron_manager.create_cron(
                name,
                description,
                instructions,
                schedule=schedule,
                schedule_timezone=timezone_name,
                run_at_utc=run_at_utc,
            )
            return f"Cron created: {cid[:8]} ({name})"
        except ValueError as e:
            return f"error: invalid cron configuration -- {e}"

    elif action == "list":
        return cron_manager.list_crons()

    elif action == "edit":
        if not cron_id:
            return "error: edit requires cron_id"
        try:
            updates = {}
            if name is not None:
                updates["name"] = name
            if description is not None:
                updates["description"] = description
            if instructions is not None:
                updates["instructions"] = instructions
            if schedule is not None:
                updates["schedule"] = schedule
                if run_at_utc is None:
                    updates["run_at_utc"] = ""
            if timezone_name is not None:
                updates["schedule_timezone"] = timezone_name
            if run_at_utc is not None:
                updates["run_at_utc"] = run_at_utc
                if schedule is None:
                    updates["schedule"] = ""

            return cron_manager.edit_cron(cron_id, **updates)
        except ValueError as e:
            return f"error: invalid cron configuration -- {e}"

    elif action == "delete":
        if not cron_id:
            return "error: delete requires cron_id"
        return cron_manager.delete_cron(cron_id)

    else:
        return f"error: unknown action '{action}'. Use create, list, edit, or delete."
