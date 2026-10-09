import pytest
from pydantic import ValidationError

from outdoor_agent.models.responses import ErrorDetail, Response
from outdoor_agent.models.routes import Geometry


@pytest.mark.parametrize(
    "kind,coordinates",
    [
        ("LineString", []),
        ("LineString", [(5, 52)]),
        ("LineString", [[(5, 52), (5.1, 52.1)]]),
        ("MultiLineString", [(5, 52), (5.1, 52.1)]),
        ("MultiLineString", [[(5, 52)]]),
        ("LineString", [(181, 52), (5, 52)]),
        ("LineString", [(5, float("nan")), (5, 52)]),
    ],
)
def test_invalid_geometry(kind: str, coordinates: object) -> None:
    with pytest.raises(ValidationError):
        Geometry.model_validate({"type": kind, "coordinates": coordinates})


@pytest.mark.parametrize(
    "value",
    [
        {"status": "error"},
        {"status": "ok"},
        {"status": "partial", "data": {}},
        {"status": "ok", "data": {}, "error": {"code": "X", "message": "x"}},
        {"status": "error", "data": {}, "error": {"code": "X", "message": "x"}},
    ],
)
def test_inconsistent_envelopes_fail(value: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Response.model_validate(value)


def test_success_error_and_partial_models() -> None:
    assert Response(status="ok", data={}).status == "ok"
    assert Response(status="error", error=ErrorDetail(code="X", message="x")).error is not None
    assert Response(status="partial", data={}, warnings=["coverage unknown"]).status == "partial"
