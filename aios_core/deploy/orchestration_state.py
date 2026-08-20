"""Process-local receipts for deterministic deployment orchestration stubs."""

from __future__ import annotations

from copy import deepcopy
from threading import RLock


class StubDeploymentReceiptStore:
    """Track which opaque IDs were actually issued by preceding stub tools."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._artifacts: dict[str, dict] = {}
        self._routes: dict[str, dict] = {}
        self._pipelines: dict[str, dict] = {}
        self._deployments: dict[str, dict] = {}
        self._latest_pipeline_by_app: dict[str, str] = {}

    def clear(self) -> None:
        """Reset all receipts. Intended for isolated stub tests."""

        with self._lock:
            self._artifacts.clear()
            self._routes.clear()
            self._pipelines.clear()
            self._deployments.clear()
            self._latest_pipeline_by_app.clear()

    def register_artifact(self, receipt: dict) -> None:
        with self._lock:
            self._artifacts[str(receipt["artifact_id"])] = deepcopy(receipt)

    def artifact(self, artifact_id: str) -> dict | None:
        with self._lock:
            receipt = self._artifacts.get(artifact_id)
            return deepcopy(receipt) if receipt is not None else None

    def register_route(self, receipt: dict) -> None:
        with self._lock:
            self._routes[str(receipt["route_id"])] = deepcopy(receipt)

    def route(self, route_id: str) -> dict | None:
        with self._lock:
            receipt = self._routes.get(route_id)
            return deepcopy(receipt) if receipt is not None else None

    def register_pipeline(self, receipt: dict) -> None:
        with self._lock:
            pipeline = deepcopy(receipt)
            pipeline_id = str(pipeline["pipeline_id"])
            self._pipelines[pipeline_id] = pipeline
            self._latest_pipeline_by_app[str(pipeline["app_id"])] = pipeline_id
            for deployment in pipeline["deployments"]:
                self._deployments[str(deployment["deployment_id"])] = {
                    **deepcopy(deployment),
                    "pipeline_id": pipeline_id,
                    "app_id": pipeline["app_id"],
                    "artifact_id": pipeline["artifact_id"],
                }

    def pipeline(self, pipeline_id: str) -> dict | None:
        with self._lock:
            receipt = self._pipelines.get(pipeline_id)
            return deepcopy(receipt) if receipt is not None else None

    def latest_pipeline(self, app_id: str) -> dict | None:
        with self._lock:
            pipeline_id = self._latest_pipeline_by_app.get(app_id)
            if pipeline_id is None:
                return None
            return deepcopy(self._pipelines[pipeline_id])

    def deployment(self, deployment_id: str) -> dict | None:
        with self._lock:
            receipt = self._deployments.get(deployment_id)
            return deepcopy(receipt) if receipt is not None else None
