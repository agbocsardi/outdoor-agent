"""Provenance-preserving GPX encoding and private, atomic export."""

import os
import tempfile
from pathlib import Path
from typing import cast

import gpxpy.gpx

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.gpx import MAX_BYTES, MAX_POINTS, inspect_gpx
from outdoor_agent.models.routes import Coordinate, Route


def route_gpx(route: Route) -> bytes:
    """Encode existing provider geometry without generating or connecting a new path."""
    if route.geometry is None:
        raise OutdoorError(
            "GEOMETRY_UNAVAILABLE", "Route has no line geometry or original GPX to export.", 5
        )
    if route.gpx_access in ("restricted", "unverified", "unavailable") and route.provider != "osm":
        raise OutdoorError(
            "EXPORT_RESTRICTED",
            "Provider export permission is not established; use its authorized export flow.",
            5,
            provider=route.provider,
        )
    lines = (
        [cast(list[Coordinate], route.geometry.coordinates)]
        if route.geometry.type == "LineString"
        else cast(list[list[Coordinate]], route.geometry.coordinates)
    )
    if sum(len(line) for line in lines) > MAX_POINTS:
        raise OutdoorError("GPX_TOO_COMPLEX", "Route geometry exceeds export point limits.")
    gpx = gpxpy.gpx.GPX()
    gpx.creator = "outdoor-agent; existing source geometry export"
    gpx.name = route.name
    gpx.description = f"{route.description or ''}\nSource: {route.source_url or route.id}\nAttribution: {route.attribution}\nProvenance: {route.source_type.value}. Original geometry; no route gaps connected.".strip()
    gpx.author_name = route.attribution
    gpx.link = route.source_url
    track = gpxpy.gpx.GPXTrack(name=route.name)
    gpx.tracks.append(track)
    for line in lines:
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points = [gpxpy.gpx.GPXTrackPoint(latitude=lat, longitude=lon) for lon, lat in line]
        track.segments.append(segment)
    content = gpx.to_xml().encode("utf-8")
    if len(content) > MAX_BYTES:
        raise OutdoorError("GPX_TOO_LARGE", "Export exceeds GPX size limits.")
    inspect_gpx(content)
    return content


def write_gpx(path: Path, content: bytes, *, overwrite: bool = False) -> None:
    """Write a private artifact, refusing to silently replace an unrelated file."""
    inspect_gpx(content)
    temporary: str | None = None
    try:
        path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        if path.exists():
            with path.open("rb") as file:
                existing = file.read(MAX_BYTES + 1)
            if existing == content:
                return
            if not overwrite:
                raise OutdoorError(
                    "OUTPUT_EXISTS",
                    "Output file already exists with different bytes; choose another path or --overwrite.",
                )
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as file:
            temporary = file.name
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            # Hard link is atomic and refuses a concurrent overwrite.
            try:
                os.link(temporary, path)
            except FileExistsError as exc:
                raise OutdoorError(
                    "OUTPUT_EXISTS", "Output appeared during export; retry with another path."
                ) from exc
            Path(temporary).unlink()
        temporary = None
    except OSError as exc:
        raise OutdoorError(
            "EXPORT_WRITE_FAILED", "Cannot write GPX output; check path permissions.", 7
        ) from exc
    finally:
        if temporary:
            Path(temporary).unlink(missing_ok=True)
