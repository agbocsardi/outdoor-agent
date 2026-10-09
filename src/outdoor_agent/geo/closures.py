"""Metric, gap-preserving closure comparisons for WGS84 route/event lines."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, radians

from pyproj import CRS, Transformer
from shapely import transform as shapely_transform
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from outdoor_agent.models.closures import ClosureEvent, ClosureFinding
from outdoor_agent.models.routes import Activity, Geometry, Route


@dataclass(frozen=True)
class SpatialMatch:
    distance_m: float
    overlap_m: float
    chainage_m: float | None
    intersects: bool


@dataclass(frozen=True)
class PreparedRoute:
    route: Route
    transformer: Transformer
    metric_crs: str
    route_lines: list[LineString]
    route_bounds: tuple[float, float, float, float]
    buffer_m: float


def _lines(geometry: Geometry) -> list[list[tuple[float, float]]]:
    if geometry.type == "LineString":
        return [geometry.coordinates]  # type: ignore[list-item]
    return geometry.coordinates  # type: ignore[return-value]


def _metric_transformer(route: Route) -> tuple[Transformer, str]:
    points = (
        [point for segment in _lines(route.geometry) for point in segment] if route.geometry else []
    )
    lon = sum(p[0] for p in points) / len(points) if points else 5.3
    lat = sum(p[1] for p in points) / len(points) if points else 52.1
    zone = min(60, max(1, int((lon + 180) / 6) + 1))
    if 3 <= lon <= 8 and 50 <= lat <= 54:
        crs = CRS.from_epsg(28992)
    elif -80 <= lat <= 84:
        crs = CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)
    else:
        crs = CRS.from_proj4(f"+proj=aeqd +lat_0={lat} +lon_0={lon} +datum=WGS84 +units=m +no_defs")
    return Transformer.from_crs("EPSG:4326", crs, always_xy=True), crs.to_string()


def prepare_route(route: Route, buffer_m: float) -> PreparedRoute | None:
    """Project route components once and cache a conservative WGS84 search envelope."""
    if route.geometry is None:
        return None
    transformer, metric_crs = _metric_transformer(route)
    route_lines = [
        shapely_transform(LineString(points), transformer.transform, interleaved=False)  # type: ignore[arg-type]
        for points in _lines(route.geometry)
    ]
    points = [point for segment in _lines(route.geometry) for point in segment]
    min_lon = min(point[0] for point in points)
    max_lon = max(point[0] for point in points)
    min_lat = min(point[1] for point in points)
    max_lat = max(point[1] for point in points)
    latitude_buffer = buffer_m / 110_574.0
    max_abs_lat = min(89.9, max(abs(min_lat), abs(max_lat)) + latitude_buffer)
    longitude_buffer = buffer_m / (111_320.0 * max(0.0017, abs(cos(radians(max_abs_lat)))))
    route_bounds = (
        min_lon - longitude_buffer,
        min_lat - latitude_buffer,
        max_lon + longitude_buffer,
        max_lat + latitude_buffer,
    )
    return PreparedRoute(
        route=route,
        transformer=transformer,
        metric_crs=metric_crs,
        route_lines=route_lines,
        route_bounds=route_bounds,
        buffer_m=buffer_m,
    )


def _event_lines(event: ClosureEvent) -> list[list[tuple[float, float]]]:
    if event.geometry is None:
        return []
    return _lines(event.geometry)


def compare_prepared(prepared: PreparedRoute, event: ClosureEvent) -> SpatialMatch | None:
    """BBox-prefilter in WGS84, then compare projected components without bridging gaps."""
    event_segments = _event_lines(event)
    if not event_segments:
        return None
    event_points = [point for segment in event_segments for point in segment]
    bounds = (
        min(point[0] for point in event_points),
        min(point[1] for point in event_points),
        max(point[0] for point in event_points),
        max(point[1] for point in event_points),
    )
    min_lon, min_lat, max_lon, max_lat = prepared.route_bounds
    event_min_lon, event_min_lat, event_max_lon, event_max_lat = bounds
    if (
        event_max_lon < min_lon
        or event_min_lon > max_lon
        or event_max_lat < min_lat
        or event_min_lat > max_lat
    ):
        return None
    event_lines = [
        shapely_transform(LineString(points), prepared.transformer.transform, interleaved=False)  # type: ignore[arg-type]
        for points in event_segments
    ]
    route_lines = prepared.route_lines
    route_cumulative = 0.0
    best_distance = float("inf")
    best_overlap = 0.0
    best_chainage: float | None = None
    intersects = False
    for route_line in route_lines:
        for event_line in event_lines:
            distance = route_line.distance(event_line)
            if distance < best_distance:
                best_distance = distance
                route_point, _ = nearest_points(route_line, event_line)
                best_chainage = route_cumulative + route_line.project(route_point)
            intersection = route_line.intersection(event_line)
            if not intersection.is_empty:
                intersects = True
                length = float(intersection.length)
                if length > best_overlap:
                    best_overlap = length
                    # Project intersection point/midpoint onto this route component.
                    candidate = (
                        intersection.interpolate(0.5, normalized=True) if length else intersection
                    )
                    if not isinstance(candidate, Point):
                        candidate, _ = nearest_points(route_line, candidate)
                    try:
                        best_chainage = route_cumulative + route_line.project(candidate)
                    except (TypeError, ValueError):
                        pass
        route_cumulative += route_line.length
    if best_distance > prepared.buffer_m and not intersects:
        return None
    return SpatialMatch(float(best_distance), best_overlap, best_chainage, intersects)


def compare_geometry(route: Route, event: ClosureEvent, buffer_m: float) -> SpatialMatch | None:
    """Convenience one-event comparison; use prepare_route for a feed of many events."""
    prepared = prepare_route(route, buffer_m)
    return compare_prepared(prepared, event) if prepared is not None else None


def _activity_modes(activity: Activity) -> set[str] | None:
    if activity in {Activity.road, Activity.gravel, Activity.mtb}:
        return {"bike"}
    if activity in {Activity.hiking, Activity.walking, Activity.trail_running, Activity.running}:
        return {"foot"}
    return None


def classify_match(
    route: Route, event: ClosureEvent, match: SpatialMatch, temporal: bool | None, buffer_m: float
) -> tuple[str, bool | None, str, list[str]]:
    modes = _activity_modes(route.activity)
    mode_overlap: bool | None = None
    if modes is not None and event.affected_modes and event.mode_evidence == "explicit":
        mode_overlap = bool(modes.intersection(event.affected_modes))
    elif modes is not None:
        mode_overlap = None

    limitations: list[str] = []
    if event.geometry_quality != "precise":
        limitations.append("Event geometry is approximate or unresolved.")
    if event.timing_quality != "precise":
        limitations.append("Event time is incomplete, recurring, or ambiguous.")
    if mode_overlap is None:
        limitations.append("Feed provides no explicit restriction evidence for this activity.")
    if match.distance_m > 0 and not match.intersects:
        limitations.append("Nearby geometry within the configured buffer is not proof of blockage.")
    if temporal is None:
        limitations.append("Event timing cannot be compared with the requested time.")
    if modes is None:
        limitations.append("Activity is not covered by this road closure mode assessment.")
    closure_type = "".join(c for c in event.restriction_type.lower() if c.isalnum())
    explicit_closure = closure_type in {
        "roadclosed",
        "carriagewayclosed",
        "carriagewayclosures",
        "roadclosure",
        "roadclosures",
        "cyclewayclosure",
        "cyclewayclosures",
        "bicycleclosure",
        "cyclistsclosed",
        "footpathclosure",
        "pedestrianclosure",
        "walkwayclosure",
        "footwayclosure",
    }
    if not explicit_closure:
        limitations.append("This traffic measure does not explicitly establish a full closure.")
    direction = "".join(c for c in (event.direction or "").lower() if c.isalnum())
    direction_uncertain = bool(direction) and direction not in {
        "both",
        "bothdirections",
        "alldirections",
        "bothways",
    }
    if direction_uncertain:
        limitations.append(
            "A directional restriction is present; route direction was not resolved."
        )

    grade_text = " ".join(
        [
            *event.grade_separation_evidence,
            event.description,
            *(str(value) for value in route.raw_metadata.values() if value is not None),
        ]
    ).lower()
    grade_separation_unresolved = any(
        clue in grade_text
        for clue in ("bridge", "overpass", "underpass", "tunnel", "viaduct", "layer=")
    )
    if grade_separation_unresolved and match.overlap_m > 0:
        limitations.append(
            "Bridge/tunnel or level evidence is present, but the route and event level relation is unresolved."
        )

    text = " ".join(
        [event.description, event.restriction_type, *event.restriction_evidence]
    ).lower()
    if mode_overlap is False:
        return (
            "possible_conflict",
            False,
            "Spatially close event has no evidence that it restricts this activity.",
            limitations,
        )
    if modes is None or temporal is None or not temporal:
        classification = (
            "possible_conflict" if temporal is None or modes is None else "coverage_unknown"
        )
        return (
            classification,
            mode_overlap,
            "Spatial event could not be confirmed as active for this activity and time.",
            limitations,
        )
    if "car only" in text or "cars only" in text or "motor vehicles only" in text:
        return (
            "possible_conflict",
            mode_overlap,
            "Vehicle-only event wording requires route-mode interpretation.",
            limitations,
        )
    if (
        match.overlap_m > 0
        and mode_overlap is True
        and event.geometry_quality == "precise"
        and event.timing_quality == "precise"
        and explicit_closure
        and not direction_uncertain
        and not grade_separation_unresolved
    ):
        return (
            "known_conflict",
            True,
            "Precise event geometry intersects the route during an explicit activity restriction.",
            limitations,
        )
    if match.distance_m <= buffer_m:
        return (
            "possible_conflict",
            mode_overlap,
            "Event is near the route or mode/geometry evidence is incomplete.",
            limitations,
        )
    return (
        "coverage_unknown",
        mode_overlap,
        "The feed contains a nearby event with insufficient evidence for a conclusion.",
        limitations,
    )


def finding_for(
    route: Route, event: ClosureEvent, match: SpatialMatch, temporal: bool | None, buffer_m: float
) -> ClosureFinding:
    classification, mode_overlap, reason, limitations = classify_match(
        route, event, match, temporal, buffer_m
    )
    return ClosureFinding(
        route_id=route.id,
        event_id=event.id,
        severity=event.severity,
        classification=classification,  # type: ignore[arg-type]
        reason=reason,
        distance_along_route_m=match.chainage_m,
        min_distance_to_route_m=match.distance_m,
        overlap_length_m=match.overlap_m if match.intersects else None,
        temporal_overlap=temporal,
        mode_overlap=mode_overlap,
        geometry_confidence="precise"
        if event.geometry_quality == "precise"
        else "approximate"
        if event.geometry is not None
        else "unknown",
        source_url=event.source_url,
        evidence=event.restriction_evidence,
        limitations=limitations,
    )
