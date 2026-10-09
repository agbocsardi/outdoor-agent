"""Geodesic measurements without bridging disconnected segments."""

from pyproj import Geod

from outdoor_agent.models.routes import Coordinate

_GEOD = Geod(ellps="WGS84")


def distance_m(a: Coordinate, b: Coordinate) -> float:
    """Distance between (longitude, latitude) coordinates on WGS84."""
    return float(_GEOD.inv(a[0], a[1], b[0], b[1])[2])


def segment_length_m(points: list[Coordinate]) -> float:
    return sum(distance_m(a, b) for a, b in zip(points, points[1:], strict=False))
