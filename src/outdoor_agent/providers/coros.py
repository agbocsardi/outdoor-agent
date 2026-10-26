"""Official manual COROS app handoff; no third-party account API calls."""

import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx, read_gpx
from outdoor_agent.models.delivery import ExportResult, HandoffResult
from outdoor_agent.models.routes import Route
from outdoor_agent.providers.base import ApprovedRequest
from outdoor_agent.providers.registry import registry
from outdoor_agent.storage.db import RouteStore

Exporter = Callable[[Settings, str, Path], Awaitable[ExportResult]]


class ManualCorosProvider:
    declaration = next(provider for provider in registry() if provider.provider == "coros")

    def __init__(
        self,
        settings: Settings,
        exporter: Exporter,
        *,
        output: Path | None = None,
        account: str | None = None,
    ):
        self.settings = settings
        self.exporter = exporter
        self.output = output
        self.account = account
        self.last_result: HandoffResult | None = None

    async def prepare(self, route: Route) -> HandoffResult:
        revision = hashlib.sha256(
            json.dumps(
                route.model_dump(mode="json", exclude={"fetched_at", "warnings", "gpx_access"}),
                sort_keys=True,
            ).encode()
        ).hexdigest()
        path = (
            self.output
            or self.settings.data_dir
            / "handoff"
            / f"{hashlib.sha256((route.id + revision).encode()).hexdigest()[:24]}.gpx"
        )
        key = hashlib.sha256(
            f"{route.id}|{revision}|coros|{self.account or 'manual'}|{path.absolute()}".encode()
        ).hexdigest()
        self.last_result = HandoffResult(
            route_id=route.id,
            account=self.account,
            status="manual_handoff_planned",
            dry_run=True,
            path=str(path.absolute()),
            source_url=route.source_url,
            idempotency_key=key,
            side_effects=[
                "Write a local GPX artifact for manual import",
                "Record the local preparation in SQLite; no third-party account write",
            ],
            instructions=[
                "Open the GPX file on your phone and choose the COROS app.",
                "Save the route in the COROS app's collection.",
                "Choose Sync With Your Device in the COROS app; verify the route on the watch.",
            ],
            warnings=[
                *route.warnings,
                "No supported automatic Ride with GPS pin/send action was verified. Nothing is submitted to COROS.",
                "GPX route navigation does not install offline map tiles.",
            ],
        )
        return self.last_result

    async def commit(self, approved_request: ApprovedRequest) -> str:
        plan = self.last_result
        if not approved_request.confirmed:
            raise OutdoorError(
                "CONFIRMATION_REQUIRED",
                "Explicit confirmation is required before GPX preparation.",
                3,
            )
        if plan is None or (
            approved_request.route_id != plan.route_id
            or approved_request.target != plan.target
            or approved_request.account != (plan.account or "manual")
            or approved_request.idempotency_key != plan.idempotency_key
        ):
            raise OutdoorError(
                "APPROVAL_MISMATCH",
                "Approval must match the prepared route, target and account.",
                3,
            )
        path = Path(plan.path)
        store = RouteStore(self.settings.data_dir)
        # Revalidate revision before writing: the approval refers to this specific plan.
        refreshed = await self.prepare(store.get_route(plan.route_id))
        if refreshed.idempotency_key != plan.idempotency_key:
            raise OutdoorError(
                "APPROVAL_MISMATCH", "Route changed after preparation; review a new plan.", 3
            )
        previous = store.delivery_record(plan.idempotency_key)
        if previous is not None and path.is_file():
            if inspect_gpx(read_gpx(path)).sha256 == previous.get("artifact_sha256"):
                self.last_result = plan.model_copy(
                    update={
                        "status": "manual_handoff_prepared",
                        "dry_run": False,
                        "already_prepared": True,
                    }
                )
                return plan.path
        exported = await self.exporter(self.settings, plan.route_id, path)
        self.last_result = plan.model_copy(
            update={
                "status": "manual_handoff_prepared",
                "dry_run": False,
                "warnings": plan.warnings + exported.warnings,
            }
        )
        audit = self.last_result.model_dump(mode="json")
        audit["artifact_sha256"] = exported.sha256
        store.record_delivery(plan.idempotency_key, plan.route_id, plan.target, audit)
        return plan.path
