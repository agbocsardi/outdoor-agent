"""Offline checks for lossless and bounded GPX inspection."""

import pytest

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import inspect_gpx, read_gpx, validate_source_url
from outdoor_agent.models.routes import Activity


def gpx(body: str) -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<gpx version="1.1" creator="synthetic test" '
        'xmlns="http://www.topografix.com/GPX/1/1">'
        f"{body}</gpx>"
    ).encode()


def test_disconnected_segments_are_not_bridged_and_use_geodesic_distance() -> None:
    content = gpx(
        "<trk><name>two separate samples</name><trkseg>"
        '<trkpt lat="52" lon="4"/><trkpt lat="52" lon="4.001"/>'
        "</trkseg><trkseg>"
        '<trkpt lat="60" lon="20"/><trkpt lat="60" lon="20.001"/>'
        "</trkseg></trk>"
    )

    result = inspect_gpx(content, activity=Activity.gravel)

    assert result.route.geometry is not None
    assert result.route.geometry.type == "MultiLineString"
    assert len(result.route.geometry.coordinates) == 2
    assert result.segment_point_counts == [2, 2]
    assert result.route.distance_m is not None
    # The two segments total about 124 m; a bridged line adds hundreds of kilometres.
    assert 120 < result.route.distance_m < 130
    assert result.route.is_loop is None
    assert "no connections across gaps were inferred" in " ".join(result.route.warnings)


def test_multitrack_routes_waypoints_and_author_are_retained() -> None:
    content = gpx(
        "<metadata><name>Synthetic outing</name>"
        "<author><name>Synthetic creator</name></author></metadata>"
        '<wpt lat="52.04" lon="4.04"><ele>2.5</ele><name>Water stop</name></wpt>'
        "<trk><name>first track</name><trkseg>"
        '<trkpt lat="52" lon="4"><ele>10</ele></trkpt>'
        '<trkpt lat="52.001" lon="4.001"><ele>20</ele></trkpt>'
        "</trkseg></trk>"
        "<trk><name>second track</name><trkseg>"
        '<trkpt lat="52.01" lon="4.01"><ele>12</ele></trkpt>'
        '<trkpt lat="52.011" lon="4.011"><ele>15</ele></trkpt>'
        "</trkseg></trk>"
        "<rte><name>return leg</name>"
        '<rtept lat="52.02" lon="4.02"><ele>5</ele></rtept>'
        '<rtept lat="52.03" lon="4.03"><ele>8</ele></rtept>'
        "</rte>"
    )

    result = inspect_gpx(content)

    assert result.track_count == 2
    assert result.track_segment_count == 2
    assert result.route_count == 1
    assert result.point_count == 7
    assert result.segment_point_counts == [2, 2, 2]
    assert result.route.geometry is not None
    assert result.route.geometry.type == "MultiLineString"
    assert result.route.attribution == "Synthetic creator"
    assert result.route.name == "Synthetic outing"
    assert result.route.start_lon == pytest.approx(4)
    assert result.route.end_lon == pytest.approx(4.03)
    assert len(result.waypoints) == 1
    assert result.waypoints[0].name == "Water stop"
    assert result.waypoints[0].lon == pytest.approx(4.04)
    assert result.waypoints[0].elevation_m == pytest.approx(2.5)
    assert result.route.elevation_gain_m == pytest.approx(16)


def test_empty_gpx_is_rejected() -> None:
    with pytest.raises(OutdoorError) as raised:
        inspect_gpx(gpx(""))

    assert raised.value.code == "EMPTY_GPX"


@pytest.mark.parametrize(
    "content",
    [
        b"<gpx><trk>",
        b"<not-gpx />",
        b'<!DOCTYPE gpx [<!ENTITY x "expanded">]><gpx>&x;</gpx>',
        b'<!DOCTYPE gpx SYSTEM "file:///etc/passwd"><gpx/>',
    ],
    ids=["malformed", "wrong-root", "internal-entity", "external-entity"],
)
def test_malformed_or_unsafe_xml_is_rejected_without_echoing_input(content: bytes) -> None:
    with pytest.raises(OutdoorError) as raised:
        inspect_gpx(content)

    assert raised.value.code == "INVALID_GPX"
    assert "passwd" not in str(raised.value)
    assert "expanded" not in str(raised.value)


@pytest.mark.parametrize(
    ("point", "expected_code"),
    [
        ('<trkpt lon="4"/>', "INVALID_GPX"),
        ('<trkpt lon="4" lat="nan"/>', "INVALID_COORDINATES"),
        ('<trkpt lon="181" lat="52"/>', "INVALID_COORDINATES"),
        ('<trkpt lon="4" lat="91"/>', "INVALID_COORDINATES"),
        ('<trkpt lon="4" lat="52"><ele>nan</ele></trkpt>', "INVALID_ELEVATION"),
    ],
    ids=[
        "missing-coordinate",
        "nan-coordinate",
        "longitude-range",
        "latitude-range",
        "nan-elevation",
    ],
)
def test_invalid_coordinates_and_elevation_are_rejected(point: str, expected_code: str) -> None:
    content = gpx(f"<trk><trkseg>{point}</trkseg></trk>")

    with pytest.raises(OutdoorError) as raised:
        inspect_gpx(content)

    assert raised.value.code == expected_code


@pytest.mark.parametrize(
    "url",
    [
        "http://ridewithgps.com/routes/1",
        "https://evil.example/routes/1",
        "https://sub.ridewithgps.com/routes/1",
        "https://user:secret@ridewithgps.com/routes/1",
        "https://ridewithgps.com:444/routes/1",
        "https://ridewithgps.com/routes/1?token=secret",
        "https://ridewithgps.com/routes/1#private",
        "https://ridewithgps.com\\@evil.example/routes/1",
    ],
)
def test_source_url_allowlist_rejects_untrusted_or_credentialed_urls(url: str) -> None:
    with pytest.raises(OutdoorError) as raised:
        validate_source_url(url)

    assert raised.value.code == "INVALID_SOURCE_URL"


def test_source_url_allowlist_accepts_supported_https_route_url() -> None:
    url = "https://ridewithgps.com/routes/12345"

    assert validate_source_url(url) == url
    assert validate_source_url(None) is None


def test_input_and_point_limits_can_be_exercised_without_large_fixtures(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import outdoor_agent.geo.gpx as parser

    path = tmp_path / "oversize.gpx"
    path.write_bytes(b"12345")
    monkeypatch.setattr(parser, "MAX_BYTES", 4)
    with pytest.raises(OutdoorError) as read_error:
        read_gpx(path)
    assert read_error.value.code == "GPX_TOO_LARGE"
    with pytest.raises(OutdoorError) as parse_error:
        inspect_gpx(b"12345")
    assert parse_error.value.code == "GPX_TOO_LARGE"

    monkeypatch.setattr(parser, "MAX_BYTES", 16 * 1024 * 1024)
    monkeypatch.setattr(parser, "MAX_POINTS", 3)
    content = gpx(
        "<trk><trkseg>"
        '<trkpt lon="4" lat="52"/><trkpt lon="4.001" lat="52"/>'
        '<trkpt lon="4.002" lat="52"/><trkpt lon="4.003" lat="52"/>'
        "</trkseg></trk>"
    )
    with pytest.raises(OutdoorError) as point_error:
        inspect_gpx(content)
    assert point_error.value.code == "GPX_TOO_COMPLEX"


def test_waypoint_only_file_has_unknown_trip_metrics() -> None:
    result = inspect_gpx(gpx('<wpt lat="52" lon="5"><name>Synthetic point</name></wpt>'))
    assert result.point_count == 1
    assert result.route.geometry is None
    assert result.route.distance_m is None
    assert result.route.is_loop is None
    assert result.route.geometry_quality == "incomplete"
    assert result.waypoints[0].name == "Synthetic point"
    assert result.route.warnings


def test_isolated_track_point_is_not_silently_added_to_geometry() -> None:
    result = inspect_gpx(
        gpx(
            '<trk><trkseg><trkpt lat="52" lon="5"/></trkseg><trkseg><trkpt lat="52" lon="5"/><trkpt lat="52" lon="5.001"/></trkseg></trk>'
        )
    )
    assert result.segment_point_counts == [1, 2]
    assert result.route.geometry_quality == "incomplete"
    assert result.route.is_loop is None
    assert result.route.warnings


def test_unsafe_embedded_link_stays_only_in_original_bytes() -> None:
    result = inspect_gpx(
        gpx(
            '<metadata><link href="https://evil.example/private?token=SECRET_SENTINEL"/></metadata><wpt lat="52" lon="5"/>'
        )
    )
    assert result.route.source_url is None
    assert "SECRET_SENTINEL" not in result.model_dump_json()
    assert any("Embedded source link" in warning for warning in result.route.warnings)
