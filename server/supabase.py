from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    publishable_key: str
    secret_key: str

    @property
    def rest_url(self) -> str:
        return f"{self.url.rstrip('/')}/rest/v1"

    def publishable_headers(self) -> dict[str, str]:
        return {
            "apikey": self.publishable_key,
            "Authorization": f"Bearer {self.publishable_key}",
        }

    def secret_headers(self) -> dict[str, str]:
        return {
            "apikey": self.secret_key,
            "Authorization": f"Bearer {self.secret_key}",
        }


def get_supabase_config() -> SupabaseConfig:
    url = os.getenv("SUPABASE_URL", "").strip()
    publishable_key = os.getenv("SUPABASE_PUBLISHABLE_KEY", "").strip()
    secret_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()

    if not url:
        raise RuntimeError("SUPABASE_URL is not configured.")
    if not publishable_key:
        raise RuntimeError("SUPABASE_PUBLISHABLE_KEY is not configured.")
    if not secret_key:
        raise RuntimeError("SUPABASE_SECRET_KEY is not configured.")

    return SupabaseConfig(
        url=url,
        publishable_key=publishable_key,
        secret_key=secret_key,
    )
