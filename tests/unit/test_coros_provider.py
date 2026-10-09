from pathlib import Path

import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx
from outdoor_agent.providers.base import ApprovedRequest, DeliveryProvider
from outdoor_agent.providers.coros import ManualCorosProvider
from outdoor_agent.services.delivery import export_route
from outdoor_agent.storage.db import RouteStore


@pytest.mark.asyncio
async def test_commit_requires_matching_approval_and_current_revision(
    tmp_path: Path, example_gpx: Path
) -> None:
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    store = RouteStore(settings.data_dir)
    route = inspect_gpx(example_gpx.read_bytes()).route.model_copy(
        update={"id": "osm:42", "provider": "osm", "provider_route_id": "42"}
    )
    store.save_route(route, example_gpx.read_bytes(), content_original=False)
    adapter = ManualCorosProvider(settings, export_route)
    provider: DeliveryProvider = adapter
    plan = await adapter.prepare(route)
    request = ApprovedRequest(
        route_id=route.id,
        target="coros",
        account="manual",
        confirmed=False,
        idempotency_key=plan.idempotency_key,
    )
    before = (settings.data_dir / "state.sqlite3").read_bytes()
    with pytest.raises(OutdoorError, match="confirmation"):
        await provider.commit(request)
    assert not Path(plan.path).exists()
    assert (settings.data_dir / "state.sqlite3").read_bytes() == before
    with pytest.raises(OutdoorError) as error:
        await provider.commit(request.model_copy(update={"confirmed": True, "account": "another"}))
    assert error.value.code == "APPROVAL_MISMATCH"
    store.save_route(route.model_copy(update={"name": "Changed after approval"}))
    with pytest.raises(OutdoorError) as error:
        await provider.commit(request.model_copy(update={"confirmed": True}))
    assert error.value.code == "APPROVAL_MISMATCH"
    assert not Path(plan.path).exists()
