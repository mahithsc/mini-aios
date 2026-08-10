from __future__ import annotations

import os
from dataclasses import dataclass


DATABASE_SCHEMA_VERSION = 1


def _integer_environment(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class ReleaseInfo:
    release_id: str
    version: str
    sequence: int
    image_digest: str | None
    revision: str | None
    database_schema: int

    def as_dict(self) -> dict[str, object]:
        return {
            "releaseId": self.release_id,
            "version": self.version,
            "sequence": self.sequence,
            "imageDigest": self.image_digest,
            "revision": self.revision,
            "databaseSchema": self.database_schema,
        }


def get_release_info() -> ReleaseInfo:
    version = os.getenv("AIOS_VERSION", "0.1.0")
    return ReleaseInfo(
        release_id=os.getenv("AIOS_RELEASE_ID", f"dev-{version}"),
        version=version,
        sequence=_integer_environment("AIOS_RELEASE_SEQUENCE", 0),
        image_digest=os.getenv("AIOS_IMAGE_DIGEST") or None,
        revision=os.getenv("AIOS_REVISION") or None,
        database_schema=DATABASE_SCHEMA_VERSION,
    )
