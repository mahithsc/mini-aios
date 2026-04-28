from __future__ import annotations

from dataclasses import dataclass

from server.supabase import get_supabase_client


class AuthError(Exception):
    pass


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: str
    email: str | None = None


def get_bearer_token(authorization: str | None) -> str:
    if not authorization:
        raise AuthError("Missing Authorization header.")

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise AuthError("Invalid Authorization header.")

    return token


def get_user_from_token(token: str | None) -> AuthenticatedUser:
    access_token = (token or "").strip()
    if not access_token:
        raise AuthError("Missing access token.")

    try:
        auth_response = get_supabase_client().auth.get_user(access_token)
    except Exception as exc:
        raise AuthError("Invalid Supabase session.") from exc

    user = getattr(auth_response, "user", None)
    if user is None:
        raise AuthError("Invalid Supabase session.")

    user_id = getattr(user, "id", None)
    if not user_id:
        raise AuthError("Supabase user id not found.")

    return AuthenticatedUser(id=user_id, email=getattr(user, "email", None))
