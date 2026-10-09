# Closure checking status and limits

The closure adapter reads the NDW current temporary-traffic-measures feed and planned
roadworks/events feed as DATEX II v3. It decodes supported two-dimensional GML lines
and retains separate indexed members as disconnected geometry segments. `WGS 84` and
`EPSG:4326` `posList` coordinates are interpreted latitude/longitude; CRS84 is
longitude/latitude. Normalized route and event coordinates remain WGS84
longitude/latitude. Location references such as Alert-C Method 2/4 remain unresolved;
when a record combines direct geometry and unresolved members, its geometry quality is
approximate so it cannot produce a known conflict. The unresolved reference identifiers
and Alert-C table/location fields are retained in report event data.

All route/event distances and intersections use a local metric projection. Dutch
routes use RD New / EPSG:28992; routes outside the Netherlands use the local UTM zone
in the correct hemisphere, with azimuthal equidistant projection near the poles.
MultiLineString components are compared separately, so distance calculations never
join missing track segments. Intersections and near misses are reported separately;
the default 20 metre buffer only raises a possible conflict. A roadworks record without an explicit closure, or a directional restriction whose direction has not been resolved, also stays possible.

For one route check, the route is projected once. Each event first receives a
conservative buffer-aware WGS84 bounding-box check; only candidates are projected and
compared in metres. This avoids projecting all feed geometry against every route point.

The assessment requires explicit mode evidence for a known conflict. Cycling activities
map to `bike`, and walking/running activities map to `foot`. A road-carriageway measure
does not automatically become a footpath restriction, and explicit nonmatching modes
are excluded. Records without mode, geometry, or time evidence remain possible or
coverage unknown. Even a successful report covers NDW road traffic measures only; it
does not establish that trails, local park paths, forestry routes or every municipality
are open.

Date-only requests cover the full local calendar day in `default_timezone`, including
23- or 25-hour daylight-saving days. Datetimes retain instant precision. Datetimes
without a timezone offset are accepted only when the local time is unique and exists;
ambiguous and nonexistent DST times need an explicit offset. Event intervals use
half-open boundaries. Feed recurrence schedules are retained as uncertain rather than
expanded into guessed active intervals.

Feed download and decompression are bounded at 24 MiB compressed and 256 MiB expanded.
XML is parsed with a streaming parser and a 100,000-record cap. A failed source,
unresolved location, stale feed, missing route geometry, or route outside NL cannot
produce a clean `no_known_conflict` result. An explicitly supplied XML file is marked
`fixture_mode` in the report and uses a `fixture://` source label.

Reports serialize the parsed request precision, effective timezone and UTC time window.
Each finding is accompanied by its matched `ClosureEvent` data, including issuer,
description, restriction/mode evidence, event interval and affected geometry where
available. Per-source errors and ages are listed separately. `unknown_coverage` remains
true when an aggregate `known_conflict` wins while other coverage is unresolved. The
feed's reliable planning horizon is not documented, so the report labels dates as
within, beyond, historical to, or unknown against the observed publication/event span;
it does not invent a fixed forecast horizon.

Unlocatable nationwide records are coverage evidence, not route-specific findings.
`coverage_summaries` groups them by source and reason with a count, at most five
sample IDs, and a SHA-256 digest covering every summarized event. At most twelve
summary rows are emitted; excess source groups fold into `other_sources` with
their full counts and evidence digests. Missing route geometry is summarized
separately. `unknown_coverage` remains true.

At most 100 route-specific findings and their corresponding events are emitted,
with known conflicts ahead of possible and unknown findings. Overflow appears
under `route_finding_limit`. The aggregate status considers all events before
truncation, so a known conflict cannot disappear from the assessment. Comparison
hashes include summary digests: changes to an unsampled event remain detectable.
Observation/publication-only refreshes do not change these digests.

## Evidence boundary

The Phase 0 read-only probe recorded in [provider-feasibility.md](provider-feasibility.md)
on 2026-10-09 fetched both configured public endpoints and observed DATEX II v3,
direct GML, and several Alert-C location types. That probe establishes endpoint
availability at that time only. It does not establish an NDW update SLA, complete
trail/cycleway coverage, or a working Alert-C resolver. The adapter intentionally does
not claim Alert-C decoding.

A read-only probe on 2026-10-09 fetched the planned feed at 15:30 UTC: 17,122,766
compressed bytes expanded to 215,034,565 bytes and contained 63,289 `situationRecord`
elements. The 256 MiB expanded limit accommodates this observed payload with bounded
headroom. The probe also observed the DATEX `forVehiclesWithCharacteristicsOf` →
`vehicleType` structure, including `bicycle`, `car`, `bus`, and `lorry` values. The
adapter uses these explicit characteristics and exact closure labels for mode evidence;
free-text mentions such as “bicycle allowed” do not establish a bicycle restriction.


Records shared by the current and planned feeds are reconciled by record-version time
(or feed publication time when absent). Newer cancellation replaces the older active
revision; conflicting revisions at the same timestamp carry uncertain validity.
Batch `recheck` fetches and parses the live feeds once and reuses those snapshots for
all selected routes. Comparison hashes include event geometry, interval, issuer,
restriction, direction, severity and findings, but exclude changing observation and
publication timestamps; refreshing an unchanged feed does not create an alert.

## Additional evidence and freshness

`--osm-restrictions` opts into a bounded Overpass query for access, foot, bicycle and construction tags. At most 1,000 ways and 16 MiB are accepted; truncation and outages retain unknown coverage. Explicit mode exemptions override generic access tags. Conditional schedules are retained without guessing their grammar, and untimed restrictions remain possible conflicts. Bridge, tunnel and layer clues prevent a two-dimensional overlap from becoming a known closure. OSM attribution and source links accompany events; no absence of tags proves a trail open.

Reports now expose `source_freshness`: last successful fetch, publication timestamp/age, cache state, ETag and Last-Modified. A usable HTTP Last-Modified is fallback freshness evidence when DATEX publicationTime is absent; collection time alone does not prove publication freshness. Structured DATEX recurrence fields and localized per-record descriptions/issuers are retained. Only explicit incompatible vehicle restrictions exclude an event: road/carriageway scope without vehicle evidence cannot establish car-only applicability.

An additional read-only Overpass probe on 2026-10-09 used Utrecht bbox west/south/east/north `5.11,52.08,5.13,52.10`: the bounded restriction request returned 599 normalized access/construction events, with no source errors and no cache reuse. This verifies retrieval/parsing at that time; it does not verify current access on any particular trail.
