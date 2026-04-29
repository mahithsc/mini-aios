from __future__ import annotations

from typing import Any

from server.auth import AuthenticatedUser
from server.supabase import get_supabase_client

PROFILES_TABLE = "profiles"


def _is_missing_optional_profile_column(exc: Exception) -> bool:
    message = str(exc)
    return "schema cache" in message and "profiles" in message and "email" in message


def ensure_profile(user: AuthenticatedUser) -> None:
    """Create or refresh the public profile for an authenticated Supabase user."""
    payload: dict[str, Any] = {
        "id": user.id,
    }
    if user.email is not None:
        payload["email"] = user.email

    try:
        get_supabase_client().table(PROFILES_TABLE).upsert(
            payload,
            on_conflict="id",
        ).execute()
    except Exception as exc:
        if "email" not in payload or not _is_missing_optional_profile_column(exc):
            raise

        get_supabase_client().table(PROFILES_TABLE).upsert(
            {"id": user.id},
            on_conflict="id",
        ).execute()
