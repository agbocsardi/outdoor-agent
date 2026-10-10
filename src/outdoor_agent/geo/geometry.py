"""Geodesic measurements without bridging disconnected segments."""

from typing import cast

from pyproj import Geod
from shapely.geometry import LineString, Point, box

from outdoor_agent.models.routes import Coordinate, Route

_GEOD = Geod(ellps="WGS84")


def distance_m(a: Coordinate, b: Coordinate) -> float:
    """Distance between (longitude, latitude) coordinates on WGS84."""
    return float(_GEOD.inv(a[0], a[1], b[0], b[1])[2])


def segment_length_m(points: list[Coordinate]) -> float:
    return sum(distance_m(a, b) for a, b in zip(points, points[1:], strict=False))


def within_bbox(route: Route, bounds: tuple[float, float, float, float]) -> bool:
    """True when route geometry intersects, or its known start lies inside, a bbox."""
    south, west, north, east = bounds
    extent = box(west, south, east, north)
    if route.geometry is None:
        if route.start_lon is None or route.start_lat is None:
            return False
        return extent.covers(Point(route.start_lon, route.start_lat))
    if route.geometry.type == "LineString":
        lines = [cast(list[Coordinate], route.geometry.coordinates)]
    else:
        lines = cast(list[list[Coordinate]], route.geometry.coordinates)
    for points in lines:
        line = LineString(points)
        if line.intersects(extent):
            return True
    return bool(
        route.start_lon is not None
        and route.start_lat is not None
        and extent.covers(Point(route.start_lon, route.start_lat))
    )
