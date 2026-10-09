"""Deterministic end-to-end CLI: GPX -> evidence -> export -> approved handoff."""

import json
from pathlib import Path

import pytest
from jsonschema import validate
from typer.testing import CliRunner

from outdoor_agent import cli
from outdoor_agent.cli import app, schemas
from outdoor_agent.geo.gpx import inspect_gpx
from outdoor_agent.services.discovery import (
    DiscoveryProviderStatus,
    DiscoveryResult,
    RankedRoute,
    RankingExplanation,
)

runner = CliRunner()


def call(args: list[str], code: int = 0) -> dict:
    result = runner.invoke(app, [*args, "--json"])
    assert result.exit_code == code, (result.output, result.exception)
    payload = json.loads(result.stdout)
    validate(payload, schemas()["response"])
    return payload


@pytest.fixture
def imported(example_gpx: Path) -> str:
    return call(["import-gpx", str(example_gpx), "--activity", "gravel"])["data"]["inspection"][
        "route"
    ]["id"]


def test_saved_routes_get_export_and_delivery(
    imported: str, example_gpx: Path, tmp_path: Path
) -> None:
    listing = call(["routes"])
    assert listing["data"]["total"] == 1
    assert listing["data"]["routes"][0]["id"] == imported
    loaded = call(["get", "--route-id", imported])
    assert loaded["data"]["route"]["id"] == imported
    planned = call(["deliver", "--route-id", imported])
    assert planned["data"]["dry_run"] is True
    assert not Path(planned["data"]["path"]).exists()
    output = tmp_path / "approved.gpx"
    prepared = call(
        [
            "deliver",
            "--route-id",
            imported,
            "--target",
            "coros",
            "--confirm",
            "--account",
            "My watch account",
            "--output",
            str(output),
        ]
    )
    validate(prepared["data"], schemas()["handoff"])
    assert prepared["data"]["status"] == "manual_handoff_prepared"
    assert output.read_bytes() == example_gpx.read_bytes()
    repeated = call(
        [
            "deliver",
            "--route-id",
            imported,
            "--confirm",
            "--account",
            "My watch account",
            "--output",
            str(output),
        ]
    )
    assert repeated["data"]["already_prepared"] is True
    assert repeated["data"]["watch_status"] == "watch_sync_unverified"
    exported = call(
        [
            "export",
            "--confirm",
            "--route-id",
            imported,
            "--format",
            "gpx",
            "--output",
            str(tmp_path / "export.gpx"),
        ]
    )
    validate(exported["data"], schemas()["export"])
    assert exported["data"]["original_bytes"] is True


def test_fixture_closure_rechecks_and_strict_json(tmp_path: Path) -> None:
    path = tmp_path / "explicit-synthetic.gpx"
    path.write_text(
        '<gpx version="1.1"><trk><trkseg><trkpt lat="52.1" lon="5.1"/><trkpt lat="52.101" lon="5.1"/></trkseg></trk></gpx>'
    )
    imported = call(["import-gpx", str(path), "--activity", "gravel"])["data"]["inspection"][
        "route"
    ]["id"]
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    args = [
        "check-closures",
        "--route-id",
        imported,
        "--date",
        "2026-10-11",
        "--feed-file",
        str(fixture),
    ]
    first = call(args)
    assessment = first["data"]["assessment"]
    validate(assessment, schemas()["closure_report"])
    assert assessment["fixture_mode"] is True
    assert assessment["status"] == "known_conflict"
    assert any(f["event_id"] == "bike-closure-record" for f in assessment["findings"])
    assert first["data"]["comparison"]["changed"] is None
    repeated = call(args)
    assert repeated["data"]["comparison"]["changed"] is False
    strict = call([*args, "--strict"], 6)
    assert strict["status"] == "partial"
    all_routes = call(["recheck", "--date", "2026-10-11", "--feed-file", str(fixture)])
    assert all_routes["data"]["total"] == 1
    assert all_routes["data"]["checks"][0]["comparison"]["changed"] is False


def test_offline_feed_outage_is_not_a_pass(imported: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OUTDOOR_AGENT_OFFLINE", "true")
    response = call(
        ["check-closures", "--route-id", imported, "--date", "2026-10-11", "--strict"], 6
    )
    assert response["status"] == "partial"
    assert response["data"]["assessment"]["status"] == "check_failed"
    assert response["data"]["assessment"]["source_errors"]


def test_search_saves_real_adapter_results_and_emits_partial(
    imported: str, example_gpx: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    route = inspect_gpx(example_gpx.read_bytes()).route.model_copy(
        update={
            "id": "osm:relation:42",
            "provider": "osm",
            "provider_route_id": "42",
            "raw_metadata": {"geometry_normalizer": "osm-v2"},
        }
    )

    async def mocked(config, criteria):
        return DiscoveryResult(
            routes=[
                RankedRoute(
                    route=route,
                    ranking=RankingExplanation(
                        score=0.9,
                        factors={"activity": 1.0},
                        explanations=["Synthetic contract adapter only"],
                    ),
                )
            ],
            provider_status=[DiscoveryProviderStatus(provider="osm", status="ok", result_count=1)],
            warnings=["Synthetic contract fixture, not a live recommendation."],
            partial=True,
        )

    monkeypatch.setattr(cli, "search_routes", mocked)
    response = call(
        [
            "search",
            "--near",
            "52.1,5.1",
            "--activity",
            "gravel",
            "--distance-km",
            "0:20",
            "--strict",
        ],
        6,
    )
    assert response["status"] == "partial"
    assert response["data"]["total"] == 1
    assert (
        call(["get", "--route-id", "osm:relation:42"])["data"]["route"]["id"] == "osm:relation:42"
    )


@pytest.mark.parametrize("date", ["bad", "2026-03-29T02:30:00", "2026-10-25T02:30:00"])
def test_invalid_and_ambiguous_dates_are_input_errors(imported: str, date: str) -> None:
    response = call(["check-closures", "--route-id", imported, "--date", date], 2)
    assert response["error"]["code"] == "INVALID_DATE"


@pytest.mark.parametrize("distance", ["20", "nan:30", "30:20", "-1:20", "20:inf"])
def test_distance_input_fails_before_network(distance: str) -> None:
    assert (
        call(["search", "--near", "Utrecht", "--distance-km", distance], 2)["error"]["code"]
        == "INVALID_DISTANCE_RANGE"
    )


def test_opt_in_places_off_by_default(imported: str) -> None:
    response = call(["places", "--route-id", imported, "--category", "viewpoint"], 5)
    assert response["error"]["code"] == "GOOGLE_DISABLED"


def test_doctor_probe_is_explicit_and_read_only() -> None:
    response = call(["doctor", "--probe"], 3)
    assert response["error"]["code"] == "AUTH_REQUIRED"


def test_disabled_providers_are_reported_and_not_called(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text('[outdoor_agent]\ndisabled_providers=["osm"]\n')
    response = call(["--config", str(config), "search", "--near", "Utrecht"], 5)
    assert response["error"]["code"] == "PROVIDERS_DISABLED"
    response = call(["--config", str(config), "providers"])
    assert not next(p for p in response["data"]["providers"] if p["provider"] == "osm")["enabled"]


@pytest.mark.parametrize(
    ("provider", "command", "error"),
    [
        ("ndw", ["check-closures", "--date", "2026-10-11"], "PROVIDER_DISABLED"),
        ("ridewithgps", ["doctor", "--probe"], "PROVIDER_DISABLED"),
        ("google", ["places"], "GOOGLE_DISABLED"),
    ],
)
def test_disabled_network_adapters_fail_before_any_request(
    imported: str, tmp_path: Path, provider: str, command: list[str], error: str
) -> None:
    config = tmp_path / "disabled.toml"
    config.write_text(f'[outdoor_agent]\ndisabled_providers=["{provider}"]\ngoogle_enabled=true\n')
    args = ["--config", str(config), *command]
    if command[0] != "doctor":
        args.extend(["--route-id", imported])
    assert call(args, 5)["error"]["code"] == error


def test_recheck_detects_changed_event_details(tmp_path: Path) -> None:
    gpx = tmp_path / "synthetic.gpx"
    gpx.write_text(
        '<gpx version="1.1"><trk><trkseg><trkpt lat="52.1" lon="5.1"/><trkpt lat="52.101" lon="5.1"/></trkseg></trk></gpx>'
    )
    imported = call(["import-gpx", str(gpx), "--activity", "gravel"])["data"]["inspection"][
        "route"
    ]["id"]
    fixture = tmp_path / "changing.xml"
    fixture.write_bytes((Path(__file__).parents[1] / "fixtures/ndw_closures.xml").read_bytes())
    args = [
        "check-closures",
        "--route-id",
        imported,
        "--date",
        "2026-10-11",
        "--feed-file",
        str(fixture),
    ]
    assert call(args)["data"]["comparison"]["changed"] is None
    assert call(args)["data"]["comparison"]["changed"] is False
    fixture.write_text(fixture.read_text().replace("Test municipality", "Updated municipality"))
    assert call(args)["data"]["comparison"]["changed"] is True


def test_batch_recheck_fetches_and_parses_feeds_once(
    imported: str, tmp_path: Path, monkeypatch
) -> None:
    from outdoor_agent.providers.ndw import FeedFetchResult, parse_fixture

    second = tmp_path / "second.gpx"
    second.write_text(
        '<gpx version="1.1"><trk><trkseg><trkpt lat="52.1" lon="5.1"/><trkpt lat="52.101" lon="5.1"/></trkseg></trk></gpx>'
    )
    call(["import-gpx", str(second), "--activity", "gravel"])
    calls = 0

    async def fetch(config):
        nonlocal calls
        calls += 1
        return FeedFetchResult(
            snapshots=[parse_fixture(Path(__file__).parents[1] / "fixtures/ndw_closures.xml")],
            errors=[],
        )

    monkeypatch.setattr(cli, "fetch_feeds", fetch)
    result = call(["recheck", "--date", "2026-10-11"])
    assert result["data"]["total"] == 2
    assert calls == 1


def test_export_requires_confirmation_and_plan_is_read_only(
    tmp_path: Path, example_gpx: Path
) -> None:
    imported = call(["import-gpx", str(example_gpx)])["data"]["inspection"]["route"]["id"]
    path = tmp_path / "approved.gpx"
    planned = call(["export", "--route-id", imported, "--output", str(path)])
    validate(planned["data"], schemas()["export_plan"])
    assert planned["data"]["dry_run"] is True
    assert planned["data"]["target"] == "local_gpx"
    assert not path.exists()
    exported = call(["export", "--route-id", imported, "--output", str(path), "--confirm"])
    assert exported["data"]["status"] == "exported"
    assert path.read_bytes() == example_gpx.read_bytes()


def test_synthetic_saved_community_catalog_discovery_to_approved_handoff(
    tmp_path: Path, example_gpx: Path
) -> None:
    """Explicit fixture import, not a claim of public/live community discovery."""
    route_id = call(
        [
            "import-gpx",
            str(example_gpx),
            "--activity",
            "gravel",
            "--source-type",
            "community",
            "--attribution",
            "Synthetic test author",
            "--source-url",
            "https://ridewithgps.com/routes/12345",
        ]
    )["data"]["inspection"]["route"]["id"]
    fixture = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    shortlisted = call(
        [
            "search",
            "--provider",
            "local",
            "--source-type",
            "community",
            "--bbox",
            "52.08,5.11,52.10,5.13",
            "--activity",
            "gravel",
            "--shape",
            "loop",
            "--date",
            "2026-10-11",
            "--feed-file",
            str(fixture),
        ]
    )
    validate(shortlisted["data"], schemas()["discovery"])
    assert shortlisted["data"]["routes"][0]["route"]["id"] == route_id
    found = shortlisted["data"]["routes"][0]
    assert found["route"]["source_type"] == "community"
    assert found["route"]["attribution"] == "Synthetic test author"
    assert found["closure_assessment"]["source_status"] == "fixture"
    assert found["closure_assessment"]["unknown_coverage"] is True
    path = tmp_path / "handoff.gpx"
    plan = call(["deliver", "--route-id", route_id, "--output", str(path)])
    assert plan["data"]["dry_run"] is True
    assert not path.exists()
    prepared = call(["deliver", "--route-id", route_id, "--output", str(path), "--confirm"])
    assert prepared["data"]["watch_status"] == "watch_sync_unverified"
    assert path.read_bytes() == example_gpx.read_bytes()


def test_import_retains_bounded_source_access_caveats_through_handoff(
    tmp_path: Path, example_gpx: Path
) -> None:
    note = "Source page flags bicycle access restrictions; verify before riding."
    imported = call(
        [
            "import-gpx",
            str(example_gpx),
            "--source-warning",
            note,
            "--source-url",
            "https://www.twcdekachelders.nl/clubtochten-gpx-mtb",
        ]
    )["data"]
    route_id = imported["inspection"]["route"]["id"]
    assert note in imported["inspection"]["route"]["warnings"]
    assert note in call(["get", "--route-id", route_id])["data"]["route"]["warnings"]
    path = tmp_path / "source-caveat-handoff.gpx"
    plan = call(["deliver", "--route-id", route_id, "--output", str(path)])
    assert note in plan["data"]["warnings"]
    prepared = call(["deliver", "--route-id", route_id, "--output", str(path), "--confirm"])
    assert note in prepared["data"]["warnings"]
    assert path.read_bytes() == example_gpx.read_bytes()
    rejected = call(["import-gpx", str(example_gpx), "--source-warning", "x" * 501], code=2)
    assert rejected["error"]["code"] == "INVALID_ARGUMENTS"


def test_recheck_detects_unsampled_coverage_changes_but_not_refresh_timestamps(
    imported: str, monkeypatch
) -> None:
    from datetime import UTC, datetime, timedelta

    from outdoor_agent.models.closures import ClosureEvent
    from outdoor_agent.providers.ndw import FeedFetchResult, FeedSnapshot
    from outdoor_agent.services.validation import assess_route as real_assess

    stamp = datetime(2026, 10, 9, tzinfo=UTC)
    events = [
        ClosureEvent(
            id=f"unresolved-{index:02d}",
            provider="ndw",
            description="Synthetic unresolved record",
            restriction_type="closure",
            status="active",
            observed_at=stamp,
            published_at=stamp,
            starts_at=stamp,
            ends_at=stamp + timedelta(days=30),
        )
        for index in range(10)
    ]

    async def fixture_assess(config, route, date, activity, **kwargs):
        return await real_assess(
            config,
            route,
            date,
            activity,
            feed_result=FeedFetchResult(
                snapshots=[
                    FeedSnapshot(
                        events=events,
                        fetched_at=stamp,
                        source_timestamp=stamp,
                        stale=False,
                        from_cache=False,
                        source="fixture://synthetic-unresolved",
                        warnings=["Synthetic test feed."],
                    )
                ],
                errors=[],
            ),
        )

    monkeypatch.setattr(cli, "assess_route", fixture_assess)
    args = ["check-closures", "--route-id", imported, "--date", "2026-10-11"]
    first = call(args)
    summary = first["data"]["assessment"]["coverage_summaries"][0]
    assert summary["count"] == 10
    assert "unresolved-09" not in summary["sample_event_ids"]
    events[:] = [
        event.model_copy(
            update={
                "observed_at": stamp + timedelta(minutes=1),
                "published_at": stamp + timedelta(minutes=1),
            }
        )
        for event in events
    ]
    refreshed = call(args)
    assert refreshed["data"]["comparison"]["changed"] is False
    events[-1] = events[-1].model_copy(
        update={"description": "Substantive change to an unsampled record"}
    )
    changed = call(args)
    assert changed["data"]["comparison"]["changed"] is True
    assert changed["data"]["assessment"]["findings"] == []
    assert changed["data"]["assessment"]["unknown_coverage"] is True
