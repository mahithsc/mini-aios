from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from ..workspace import get_runtime_paths
from .manifest import load_manifest, manifest_to_dict
from .models import AppManifest, AppOrigin, AppRecord
from .registry import AppLifecycleError
from .runtime import AppRuntime, DockerUnavailableError, RuntimeResult, StopResult
from .service import AppService


def _runtime_payload(result: RuntimeResult | StopResult) -> dict[str, object]:
    return asdict(result)


def _requires_container(manifest: AppManifest | None) -> bool:
    return bool(
        manifest and (manifest.prepare or manifest.mcp_servers or manifest.executables)
    )


class AppCoordinator:
    """Shared lifecycle facade for the agent tool and HTTP API."""

    def __init__(
        self,
        *,
        service: AppService | None = None,
        runtime: AppRuntime | None = None,
    ) -> None:
        self.service = service or AppService()
        paths = get_runtime_paths()
        self.runtime = runtime or AppRuntime(paths.state / "apps")

    @property
    def registry(self):
        return self.service.registry

    def create(
        self,
        slug: str,
        *,
        name: str | None = None,
        description: str = "",
        version: str = "0.1.0",
        origin: AppOrigin | str = AppOrigin.AGENT,
        created_by_chat_id: str | None = None,
        created_by_run_id: str | None = None,
    ) -> dict[str, object]:
        self._close_host_processes()
        app = self.service.create_app(
            slug,
            name=name,
            description=description,
            version=version,
            origin=origin,
            created_by_chat_id=created_by_chat_id,
            created_by_run_id=created_by_run_id,
        )
        return {
            "app": app.to_dict(),
            "editablePath": str(self.service.editable_path(app)),
        }

    def register(
        self,
        slug: str,
        *,
        origin: AppOrigin | str = AppOrigin.USER,
        created_by_chat_id: str | None = None,
        created_by_run_id: str | None = None,
    ) -> dict[str, object]:
        self._close_host_processes()
        app = self.service.register_app(
            slug,
            origin=origin,
            created_by_chat_id=created_by_chat_id,
            created_by_run_id=created_by_run_id,
        )
        return {
            "app": app.to_dict(),
            "editablePath": str(self.service.editable_path(app)),
        }

    def list(self) -> dict[str, object]:
        return {"apps": [app.to_dict() for app in self.registry.list()]}

    def inspect(self, app_id_or_slug: str) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        active_manifest = self.active_manifest(app)
        return {
            "app": app.to_dict(),
            "editablePath": str(self.service.editable_path(app)),
            "manifest": manifest_to_dict(app.manifest) if app.manifest else None,
            "activeManifest": (
                manifest_to_dict(active_manifest) if active_manifest else None
            ),
        }

    def validate(self, app_id_or_slug: str) -> dict[str, object]:
        validated = self.service.validate(app_id_or_slug)
        return {
            "app": validated.app.to_dict(),
            "snapshot": {
                "contentHash": validated.snapshot.content_hash,
                "fileCount": validated.snapshot.file_count,
                "sizeBytes": validated.snapshot.size_bytes,
            },
            "manifest": manifest_to_dict(validated.manifest),
        }

    def prepare(
        self,
        app_id_or_slug: str,
        *,
        approve_network: bool = False,
    ) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        if app.validated_hash is None or app.manifest is None:
            raise AppLifecycleError(
                "an App must be validated before it can be prepared"
            )
        if approve_network and not app.network_approved:
            app = self.service.set_network_approved(app.id, True)
        snapshot_path = self.service.snapshot_path(app, app.validated_hash)
        snapshot = {
            "path": snapshot_path,
            "content_hash": app.validated_hash,
        }
        try:
            result = self.runtime.prepare(
                app,
                snapshot,
                network_approved=app.network_approved,
            )
        except Exception as exc:
            self.registry.record_error(app.id, str(exc))
            raise
        if not result.ok:
            message = result.stderr or result.stdout or "App preparation failed"
            self.registry.record_error(app.id, message)
            return {
                "app": self.registry.require(app.id).to_dict(),
                "runtime": _runtime_payload(result),
            }
        prepared = self.service.mark_prepared(app.id, app.validated_hash)
        return {
            "app": prepared.to_dict(),
            "runtime": _runtime_payload(result),
        }

    def enable(self, app_id_or_slug: str) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        if app.validated_hash is None or app.prepared_hash != app.validated_hash:
            raise AppLifecycleError(
                "the current App snapshot must be prepared before enabling"
            )
        if app.manifest and app.manifest.runtime.network and not app.network_approved:
            raise AppLifecycleError(
                "App network access requires explicit approval before enabling"
            )
        if _requires_container(app.manifest) and not self.runtime.available():
            raise DockerUnavailableError(
                "Docker is unavailable; executable Apps cannot be enabled"
            )
        if app.active_hash != app.validated_hash:
            app = self.service.activate(app.id, app.validated_hash, enable=True)
        elif not app.enabled:
            app = self.service.set_enabled(app.id, True)
        return {"app": app.to_dict()}

    def disable(self, app_id_or_slug: str) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        stop: StopResult | None = None
        active_manifest = self.active_manifest(app)
        if _requires_container(active_manifest):
            stop = self.runtime.stop_app(app.id)
            if not stop.ok:
                raise AppLifecycleError(
                    stop.stderr or "App containers could not be stopped"
                )
        if app.enabled:
            app = self.service.set_enabled(app.id, False)
        result: dict[str, object] = {"app": app.to_dict()}
        if stop is not None:
            result["runtime"] = _runtime_payload(stop)
        return result

    def set_network_approved(
        self,
        app_id_or_slug: str,
        approved: bool,
    ) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        stopped: dict[str, object] | None = None
        if not approved and app.enabled:
            stopped = self.disable(app.id).get("runtime")
        app = self.service.set_network_approved(app.id, approved)
        result: dict[str, object] = {"app": app.to_dict()}
        if stopped is not None:
            result["runtime"] = stopped
        return result

    def run(
        self,
        app_id_or_slug: str,
        executable_id: str,
        args: Sequence[object] = (),
    ) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        if not app.enabled or not app.active_hash:
            raise AppLifecycleError("App must be enabled before running an executable")
        manifest = self.active_manifest(app)
        executable = next(
            (
                candidate
                for candidate in (manifest.executables if manifest else ())
                if candidate.id == executable_id
            ),
            None,
        )
        if executable is None:
            raise AppLifecycleError(
                f"executable is not declared by the active App: {executable_id}"
            )
        result = self.runtime.run_executable(
            app,
            executable,
            args,
            network_approved=app.network_approved,
        )
        return {
            "app": app.to_dict(),
            "executable": executable.id,
            "runtime": _runtime_payload(result),
        }

    def unregister(self, app_id_or_slug: str) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        editable_path = self.service.editable_path(app)
        self.disable(app.id)
        removed = self.registry.unregister(app.id)
        return {
            "app": removed.to_dict(),
            "unregistered": True,
            "sourcePreserved": editable_path.exists(),
        }

    def reset_data(self, app_id_or_slug: str) -> dict[str, object]:
        app = self.registry.require(app_id_or_slug)
        if app.enabled:
            self.disable(app.id)
            app = self.registry.require(app.id)
        cleared = self.runtime.clear_data(app.id)
        return {
            "app": app.to_dict(),
            "dataReset": cleared,
        }

    def active_manifest(self, app: AppRecord | str) -> AppManifest | None:
        record = self.registry.require(app) if isinstance(app, str) else app
        if not record.active_hash:
            return None
        manifest_path = (
            self.service.snapshot_path(record, record.active_hash) / "app.json"
        )
        return load_manifest(manifest_path)

    def active_snapshot_path(self, app: AppRecord | str) -> Path:
        record = self.registry.require(app) if isinstance(app, str) else app
        if not record.active_hash:
            raise AppLifecycleError("App has no active snapshot")
        return self.service.snapshot_path(record, record.active_hash)

    @staticmethod
    def _close_host_processes() -> None:
        from ..tools.processes import close_all_processes

        close_all_processes()
