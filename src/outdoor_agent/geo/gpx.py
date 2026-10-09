"""Bounded, secure parsing of original GPX without geometry repair."""

import hashlib
import math
from pathlib import Path
from urllib.parse import urlsplit

import gpxpy
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from gpxpy.gpx import GPXRoutePoint, GPXTrackPoint, GPXWaypoint

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.geometry import distance_m, segment_length_m
from outdoor_agent.models.routes import (
    Activity,
    Coordinate,
    Geometry,
    GPXInspection,
    Route,
    SourceType,
    Waypoint,
)

MAX_BYTES = 16 * 1024 * 1024
MAX_POINTS = 100_000
SOURCE_HOSTS = frozenset(
    {
        "ridewithgps.com",
        "www.ridewithgps.com",
        "wikiloc.com",
        "www.wikiloc.com",
        "alltrails.com",
        "www.alltrails.com",
        "strava.com",
        "www.strava.com",
        "openstreetmap.org",
        "www.openstreetmap.org",
    }
)
Point = GPXTrackPoint | GPXRoutePoint | GPXWaypoint


def validate_source_url(value: str | None) -> str | None:
    """Accept attribution links only; never fetch them or retain URL credentials."""
    if value is None:
        return None
    try:
        url = urlsplit(value)
        valid = (
            url.scheme == "https"
            and url.hostname in SOURCE_HOSTS
            and url.username is None
            and url.password is None
            and url.port in (None, 443)
            and not url.query
            and not url.fragment
            and not any(ord(char) < 33 for char in value)
            and "\\" not in value
        )
    except ValueError:
        valid = False
    if not valid:
        raise OutdoorError(
            "INVALID_SOURCE_URL",
            "Use an HTTPS route URL from a supported source without credentials or query parameters.",
        )
    return value


def read_gpx(path: Path) -> bytes:
    """Read at most the documented payload bound; errors never include file contents."""
    try:
        if not path.is_file():
            raise OutdoorError("GPX_NOT_FOUND", "GPX input must be a readable regular file.")
        with path.open("rb") as file:
            content = file.read(MAX_BYTES + 1)
    except OSError as exc:
        raise OutdoorError("GPX_UNREADABLE", "Could not read the GPX input file.") from exc
    if len(content) > MAX_BYTES:
        raise OutdoorError("GPX_TOO_LARGE", "GPX exceeds the 16 MiB limit.")
    return content


def _coordinate(point: Point) -> Coordinate:
    lon, lat = point.longitude, point.latitude
    if lon is None or lat is None or not math.isfinite(lon) or not math.isfinite(lat):
        raise OutdoorError("INVALID_COORDINATES", "GPX coordinates must be finite numbers.")
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise OutdoorError("INVALID_COORDINATES", "GPX coordinates are outside WGS84 bounds.")
    if point.elevation is not None and not math.isfinite(point.elevation):
        raise OutdoorError("INVALID_ELEVATION", "GPX elevation must be finite when supplied.")
    return lon, lat


def inspect_gpx(
    content: bytes,
    *,
    activity: Activity = Activity.other,
    source_url: str | None = None,
    source_type: SourceType = SourceType.unknown,
    attribution: str | None = None,
) -> GPXInspection:
    """Inspect tracks, routes, and waypoints, preserving gaps and nullable elevations."""
    source_url = validate_source_url(source_url)
    if len(content) > MAX_BYTES:
        raise OutdoorError("GPX_TOO_LARGE", "GPX exceeds the 16 MiB limit.")
    try:
        root = ElementTree.fromstring(content, forbid_dtd=True)
        if root.tag not in (
            "gpx",
            "{http://www.topografix.com/GPX/1/0}gpx",
            "{http://www.topografix.com/GPX/1/1}gpx",
        ):
            raise OutdoorError("INVALID_GPX", "Expected a GPX document root.")
        point_count = sum(
            element.tag.rsplit("}", 1)[-1] in {"trkpt", "rtept", "wpt"} for element in root.iter()
        )
        if point_count > MAX_POINTS:
            raise OutdoorError("GPX_TOO_COMPLEX", "GPX exceeds the 100,000 point limit.")
        gpx = gpxpy.parse(content.decode("utf-8-sig"))
    except (
        DefusedXmlException,
        ElementTree.ParseError,
        UnicodeError,
        gpxpy.gpx.GPXException,
        ValueError,
        TypeError,
    ) as exc:
        raise OutdoorError("INVALID_GPX", "GPX is malformed, unsafe, or not UTF-8 XML.") from exc

    segments: list[list[Point]] = [
        list(segment.points) for track in gpx.tracks for segment in track.segments
    ]
    segments.extend(list(route.points) for route in gpx.routes)
    coordinate_segments = [[_coordinate(point) for point in segment] for segment in segments]
    waypoint_coordinates = [_coordinate(point) for point in gpx.waypoints]
    path_coordinates = [point for segment in coordinate_segments for point in segment]
    all_coordinates = path_coordinates + waypoint_coordinates
    if not all_coordinates:
        raise OutdoorError("EMPTY_GPX", "GPX contains no track, route, or waypoint coordinates.")

    warnings = []
    if source_url is None and gpx.link:
        try:
            source_url = validate_source_url(gpx.link)
        except OutdoorError:
            warnings.append(
                "Embedded source link was not a supported safe route URL; original bytes retain it."
            )
    if any(len(segment) < 2 for segment in segments):
        warnings.append(
            "Empty or single-point segments retained in counts; cannot form line geometry."
        )
    lines = [segment for segment in coordinate_segments if len(segment) >= 2]
    if len(lines) > 1:
        warnings.append("Multiple segments retained; no connections across gaps were inferred.")
    if gpx.tracks and gpx.routes:
        warnings.append(
            "GPX contains tracks and routes; segment totals may include alternative representations of the same trip."
        )
    if not lines:
        warnings.append(
            "No navigable line geometry; GPX contains only isolated points or waypoints."
        )
    if gpx.author_name and attribution is None:
        attribution = gpx.author_name
    names = [gpx.name] + [track.name for track in gpx.tracks] + [route.name for route in gpx.routes]
    name = next((name for name in names if name), "Imported GPX")
    elevations_complete = bool(path_coordinates) and all(
        point.elevation is not None for segment in segments for point in segment
    )
    elevation_gain = None
    if elevations_complete:
        elevation_gain = sum(
            max(0.0, float(b.elevation) - float(a.elevation))
            for segment in segments
            for a, b in zip(segment, segment[1:], strict=False)
            if a.elevation is not None and b.elevation is not None
        )
    elif path_coordinates:
        warnings.append("Elevation gain unknown because elevation samples are missing.")
    # Loop status is unknown for multiple segments; connecting them could change the trip.
    is_loop = (
        distance_m(lines[0][0], lines[0][-1]) <= 20
        if len(lines) == 1 and len(segments) == 1
        else None
    )
    digest = hashlib.sha256(content).hexdigest()
    geometry = None
    if len(lines) == 1:
        geometry = Geometry(type="LineString", coordinates=lines[0])
    elif lines:
        geometry = Geometry(type="MultiLineString", coordinates=lines)
    start = path_coordinates[0] if path_coordinates else None
    end = path_coordinates[-1] if path_coordinates else None
    route = Route(
        id=f"local:{digest}",
        provider="local",
        provider_route_id=digest,
        source_url=source_url,
        source_type=source_type,
        attribution=attribution or "User-provided GPX; creator unverified",
        name=name,
        description=gpx.description,
        activity=activity,
        geometry=geometry,
        start_lon=start[0] if start else None,
        start_lat=start[1] if start else None,
        end_lon=end[0] if end else None,
        end_lat=end[1] if end else None,
        distance_m=sum(segment_length_m(segment) for segment in coordinate_segments)
        if lines
        else None,
        elevation_gain_m=elevation_gain,
        is_loop=is_loop,
        geometry_quality=("disconnected" if len(lines) > 1 else "complete")
        if lines and all(len(s) >= 2 for s in segments)
        else "incomplete",
        gpx_access="available",
        warnings=warnings,
    )
    return GPXInspection(
        route=route,
        sha256=digest,
        byte_count=len(content),
        bounds=(
            min(p[0] for p in all_coordinates),
            min(p[1] for p in all_coordinates),
            max(p[0] for p in all_coordinates),
            max(p[1] for p in all_coordinates),
        ),
        track_count=len(gpx.tracks),
        track_segment_count=sum(len(t.segments) for t in gpx.tracks),
        route_count=len(gpx.routes),
        point_count=len(all_coordinates),
        segment_point_counts=[len(s) for s in segments],
        waypoints=[
            Waypoint(
                lon=point.longitude,
                lat=point.latitude,
                elevation_m=point.elevation,
                name=point.name,
            )
            for point in gpx.waypoints
        ],
    )
