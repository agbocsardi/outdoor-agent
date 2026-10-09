"""Transparent, source-neutral ranking and conservative route deduplication."""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from typing import cast

from pyproj import Geod

from outdoor_agent.models.routes import Activity, Coordinate, Route, SourceType

GEOD = Geod(ellps="WGS84")


@dataclass(frozen=True)
class SourceProvenance:
    provider: str
    id: str
    provider_route_id: str
    source_url: str | None
    attribution: str
    source_type: SourceType


@dataclass(frozen=True)
class ScoredRoute:
    route: Route
    score: float
    factors: dict[str, float]
    explanations: list[str]
    aliases: list[str]
    provenance: list[SourceProvenance]


def rank_routes(
    routes: list[Route],
    *,
    activity: Activity,
    center: Coordinate,
    radius_km: float,
    distance_min_m: float | None,
    distance_max_m: float | None,
    loop: str,
    weights: dict[str, float],
) -> list[ScoredRoute]:
    """Rank only observable factors; absent popularity/freshness stays out of denominator."""
    deduped = deduplicate_routes(routes)
    ranked: list[ScoredRoute] = []
    for route, aliases, provenance in deduped:
        if route.start_lat is None or route.start_lon is None:
            continue
        proximity_km = _distance_km(center, (route.start_lon, route.start_lat))
        if proximity_km > radius_km:
            continue
        if route.distance_m is None and (distance_min_m is not None or distance_max_m is not None):
            continue
        if route.distance_m is not None and (
            (distance_min_m is not None and route.distance_m < distance_min_m)
            or (distance_max_m is not None and route.distance_m > distance_max_m)
        ):
            continue
        if loop == "required" and route.is_loop is not True:
            continue
        factors: dict[str, float] = {
            "proximity": max(0.0, 1.0 - proximity_km / radius_km),
            "activity": _activity_score(activity, route.activity),
        }
        explanations = [f"Start is {proximity_km:.1f} km from the requested location."]
        if route.distance_m is not None and (
            distance_min_m is not None or distance_max_m is not None
        ):
            if distance_min_m is not None and distance_max_m is not None:
                midpoint = (distance_min_m + distance_max_m) / 2
                half_range = max((distance_max_m - distance_min_m) / 2, 1)
                factors["distance"] = max(0.0, 1.0 - abs(route.distance_m - midpoint) / half_range)
            elif distance_min_m is not None:
                factors["distance"] = min(1.0, route.distance_m / distance_min_m)
            elif distance_max_m is not None:
                factors["distance"] = max(0.0, 1.0 - route.distance_m / distance_max_m)
            explanations.append(f"Mapped length is {route.distance_m / 1000:.1f} km.")
        else:
            explanations.append(
                "Mapped length is unknown; no length score was applied."
                if route.distance_m is None
                else "No distance preference was supplied; no length score was applied."
            )
        explanations.append(f"Activity match score is {factors['activity']:.2f}.")
        if loop != "any" and route.is_loop is not None:
            factors["loop"] = float(route.is_loop)
            explanations.append(
                "Mapped geometry appears to be a loop."
                if route.is_loop
                else "Mapped geometry is not a loop."
            )
        if route.popularity is not None:
            factors["popularity"] = _bounded_metric(route.popularity.value)
            explanations.append("A source-provided popularity metric contributed to ranking.")
        else:
            explanations.append("Popularity is unavailable and was excluded from the score.")
        if route.updated_at is not None:
            age_days = max(0.0, (route.fetched_at - route.updated_at).total_seconds() / 86400)
            factors["freshness"] = math.exp(-age_days / 365.0)
            explanations.append("Freshness uses the provider's last-updated timestamp.")
        else:
            explanations.append("Provider update time is unavailable; freshness was excluded.")
        explanations.append("Closure risk is unassessed; use check-closures for the planned date.")
        if weights.get("scenic", 0) > 0:
            explanations.append("Scenic places are not enriched during search and were excluded.")
        denominator = sum(weights.get(key, 0.0) for key in factors)
        score = (
            sum(value * weights.get(key, 0.0) for key, value in factors.items()) / denominator
            if denominator
            else 0.0
        )
        ranked.append(
            ScoredRoute(route, round(score, 4), factors, explanations, aliases, provenance)
        )
    return sorted(ranked, key=lambda item: (-item.score, item.route.name.casefold(), item.route.id))


def deduplicate_routes(
    routes: list[Route],
) -> list[tuple[Route, list[str], list[SourceProvenance]]]:
    """Merge only exact geometry fingerprints or strong name/location/length matches."""
    groups: list[tuple[Route, list[str], list[SourceProvenance], str | None]] = []
    for route in routes:
        fingerprint = geometry_fingerprint(route)
        matched: tuple[Route, list[str], list[SourceProvenance], str | None] | None = None
        for existing in groups:
            if (
                fingerprint
                and existing[3] == fingerprint
                and _normal_name(route.name) == _normal_name(existing[0].name)
                and route.activity == existing[0].activity
                and route.source_type == existing[0].source_type
            ):
                matched = existing
                break
            if _strong_match(route, existing[0]):
                matched = existing
                break
        if matched is None:
            groups.append((route, [], [_source_provenance(route)], fingerprint))
        elif route.id != matched[0].id:
            matched[1].append(route.source_url or route.id)
            matched[2].append(_source_provenance(route))
    return [(route, aliases, provenance) for route, aliases, provenance, _ in groups]


def _source_provenance(route: Route) -> SourceProvenance:
    return SourceProvenance(
        provider=route.provider,
        id=route.id,
        provider_route_id=route.provider_route_id,
        source_url=route.source_url,
        attribution=route.attribution,
        source_type=route.source_type,
    )


def geometry_fingerprint(route: Route) -> str | None:
    geometry = route.geometry
    if geometry is None:
        return None
    coordinates = geometry.coordinates
    lines = (
        [cast(list[Coordinate], coordinates)]
        if geometry.type == "LineString"
        else cast(list[list[Coordinate]], coordinates)
    )
    normalized: list[tuple[tuple[float, float], ...]] = []
    for line in lines:
        points = tuple((round(lon, 5), round(lat, 5)) for lon, lat in line)
        normalized.append(min(points, tuple(reversed(points))))
    payload = repr(sorted(normalized)).encode()
    return hashlib.sha256(payload).hexdigest()


def _strong_match(a: Route, b: Route) -> bool:
    if a.activity != b.activity or a.source_type != b.source_type:
        return False
    names = {_normal_name(a.name), _normal_name(b.name)}
    if len(names) != 1 or not next(iter(names)):
        return False
    if any(
        value is None
        for value in (
            a.start_lat,
            a.start_lon,
            b.start_lat,
            b.start_lon,
            a.distance_m,
            b.distance_m,
        )
    ):
        return False
    assert a.start_lat is not None and a.start_lon is not None
    assert b.start_lat is not None and b.start_lon is not None
    assert a.distance_m is not None and b.distance_m is not None
    lat_a, lon_a, lat_b, lon_b = a.start_lat, a.start_lon, b.start_lat, b.start_lon
    distance_a, distance_b = a.distance_m, b.distance_m
    separation = _distance_km((lon_a, lat_a), (lon_b, lat_b))
    ratio = abs(distance_a - distance_b) / max(distance_a, distance_b, 1)
    return separation <= 0.1 and ratio <= 0.03


def _normal_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def _distance_km(a: Coordinate, b: Coordinate) -> float:
    return abs(float(GEOD.inv(a[0], a[1], b[0], b[1])[2])) / 1000


def _activity_score(requested: Activity, actual: Activity) -> float:
    if requested == actual:
        return 1.0
    compatible = {
        Activity.walking: {Activity.hiking},
        Activity.hiking: {Activity.walking},
        Activity.running: {Activity.trail_running, Activity.hiking},
        Activity.trail_running: {Activity.running, Activity.hiking},
        Activity.road: {Activity.gravel},
        Activity.gravel: {Activity.road, Activity.mtb},
        Activity.mtb: {Activity.gravel},
        Activity.other: set(),
    }
    return 0.65 if actual in compatible[requested] else 0.0


def _bounded_metric(value: float) -> float:
    """Normalize nonnegative count-like metrics without treating zero as missing."""
    return max(0.0, min(1.0, math.log1p(max(0.0, value)) / math.log1p(100)))
