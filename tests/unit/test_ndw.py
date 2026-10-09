from datetime import UTC, datetime
from pathlib import Path

from defusedxml import ElementTree

from outdoor_agent.providers.ndw import (
    _geometry,
    _location_reference,
    _modes,
    parse_feed,
    parse_fixture,
)


def test_ndw_decodes_direct_gml_and_keeps_alertc_unresolved() -> None:
    path = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    feed = parse_fixture(path)
    by_id = {event.id: event for event in feed.events}
    bike = by_id["bike-closure-record"]
    assert bike.geometry is not None
    assert bike.geometry.coordinates == [(5.1, 52.1), (5.1, 52.101)]
    assert bike.affected_modes == ["bike"]
    assert bike.issuer == "Test municipality"
    assert bike.starts_at == datetime(2026, 10, 10, tzinfo=UTC)
    assert by_id["alert-c-record"].geometry is None
    assert "Alert-C was not decoded" in feed.warnings[0]
    assert feed.fixture_mode is True


def test_rejects_wrong_datex_version() -> None:
    content = b'<mc:messageContainer xmlns:mc="x" modelBaseVersion="2" />'
    try:
        parse_feed(content)
    except ValueError as exc:
        assert "v3" in str(exc)
    else:
        raise AssertionError("v2 must not be advertised as parsed")


def test_negative_free_text_does_not_claim_bicycle_restriction() -> None:
    record = ElementTree.fromstring(
        b"<record><comment>bicycle allowed; cyclepathopen</comment></record>"
    )
    assert _modes(record, "RoadOrCarriagewayOrLaneManagement", "bicycle allowed") == ["road"]


def test_explicit_datex_vehicle_type_identifies_bicycle_restriction() -> None:
    record = ElementTree.fromstring(
        b"<record><forVehiclesWithCharacteristicsOf><vehicleType>bicycle</vehicleType></forVehiclesWithCharacteristicsOf></record>"
    )
    assert _modes(record, "RoadOrCarriagewayOrLaneManagement", "carriagewayClosures") == [
        "bike",
        "road",
    ]


def test_record_limit_checks_before_extending_event_list(monkeypatch) -> None:
    import outdoor_agent.providers.ndw as ndw

    path = Path(__file__).parents[1] / "fixtures/ndw_closures.xml"
    monkeypatch.setattr(ndw, "MAX_EVENTS", 1)
    try:
        parse_fixture(path)
    except ValueError as exc:
        assert "100,000" in str(exc)
    else:
        raise AssertionError("record count limit must reject oversized feed")


def test_multiple_direct_members_remain_disconnected_and_crs84_is_lon_lat() -> None:
    record = ElementTree.fromstring(
        b"""<record xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <locationContainedInItinerary><location xsi:type="LinearLocation"><gmlLineString srsName="urn:ogc:def:crs:OGC:1.3:CRS84"><posList>5.1 52.1 5.11 52.1</posList></gmlLineString></location></locationContainedInItinerary>
          <locationContainedInItinerary><location xsi:type="LinearLocation"><gmlLineString srsName="EPSG:4326"><posList>52.2 5.2 52.21 5.2</posList></gmlLineString></location></locationContainedInItinerary>
        </record>"""
    )
    geometry, quality, problem = _geometry(record)
    assert geometry is not None and geometry.type == "MultiLineString"
    assert geometry.coordinates == [[(5.1, 52.1), (5.11, 52.1)], [(5.2, 52.2), (5.2, 52.21)]]
    assert quality == "precise"
    assert problem is None


def test_gml_plus_alertc_is_retained_as_approximate_with_reference_data() -> None:
    record = ElementTree.fromstring(
        b"""<record xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
          <locationReference xsi:type="ItineraryByIndexedLocations">
            <locationContainedInItinerary><location xsi:type="LinearLocation"><gmlLineString srsName="WGS 84"><posList>52.1 5.1 52.11 5.1</posList></gmlLineString></location></locationContainedInItinerary>
            <locationContainedInItinerary><location xsi:type="SingleRoadLinearLocation"><alertCLinear xsi:type="AlertCMethod4Linear"><alertCLocationCountryCode>8</alertCLocationCountryCode><alertCLocationTableNumber>6.13</alertCLocationTableNumber><alertCLocationTableVersion>A</alertCLocationTableVersion><specificLocation>7979</specificLocation></alertCLinear></location></locationContainedInItinerary>
          </locationReference>
        </record>"""
    )
    geometry, quality, problem = _geometry(record)
    reference, reference_types = _location_reference(
        next(e for e in record.iter() if e.tag == "locationReference")
    )
    assert geometry is not None
    assert quality == "approximate"
    assert problem is not None
    assert "AlertCMethod4Linear" in (reference_types or "")
    assert "specificLocation=7979" in (reference or "")
    assert "alertCLocationTableNumber=6.13" in (reference or "")
