# Route discovery and OSM use

## Current scope

The discovery service can search mapped OpenStreetMap route relations through
Overpass. Those records are labeled `official_trail`: a mapped hiking or cycling
route is not evidence that a community rode it, recommends it, or reviewed it.
There is no verified public community-route catalog configured. A public community-only
request returns `COMMUNITY_DISCOVERY_UNAVAILABLE`; a mixed/default request can
return OSM trails with a warning about this limitation. No fixture is used as a
live result.

## Read-only probe evidence

On 2026-10-09, a read-only Nominatim lookup for `Utrecht, Netherlands` returned
WGS84 latitude `52.0907006`, longitude `5.1215634` from
`https://nominatim.openstreetmap.org/search`. A bounded Overpass POST to
`https://overpass-api.de/api/interpreter`, for bbox
`52.08,5.11,52.10,5.13`, returned 22 matching hiking relations. The first three
were relation IDs `93841` (Utrechtpad etappe01), `534549` (Waterlinie - Lunetten
/ Schalkwijkse Wetering), and `571664` (Waterlinie - Houtense Vlakte).
All 22 had some usable member geometry; 12 were incomplete under the adapter's
conservative completeness check. Overpass runtime remarks and relation-limit
truncation surface as partial-result warnings; an empty result with a runtime
remark is a typed provider failure.

This verifies that these public read endpoints responded to these example
requests on that date. It does not establish uptime, complete geographic
coverage, route access conditions, public community search, GPX download rights,
or a recommendation/popularity signal. Mapped geometry, names, route tags and
endpoint behavior can change after the probe.

## Query and geometry handling

Search converts the requested radius into a WGS84 bounding box using geodesic
cardinal extents. It narrows route tags to the requested activity and asks
Overpass for at most 200 relations. Search radius is bounded to 100 km; callers
should make smaller local searches. An extent crossing the antimeridian returns a
typed unsupported-bounds error because the current Overpass query uses one
non-wrapping bbox. Reaching the relation limit warns that additional matches may
be omitted. The request uses a public Overpass POST and the configured identifying
HTTP User-Agent. Responses are payload-bounded and locally cached using
`discovery_cache_ttl_seconds`. Provider status reports cache use and staleness;
stale cache use is also a search warning. A single OSM relation can be fetched
with `OSMProvider.get` using its numeric `provider_route_id`.

Relation member ways are reconstructed in their listed order. A `backward` role
reverses its member geometry; other ways are joined at shared endpoints and may
be reversed when the endpoints make the connection unambiguous. Disconnected ways
remain separate lines in a `MultiLineString`. The service never draws a line
across a gap. Missing member geometry marks the geometry incomplete, so route
length and loop shape are left unknown. Length for complete or disconnected
geometry is the sum of WGS84 geodesic segment lengths, excluding gaps. Elevation,
popularity, rating and review count remain unknown because this query does not
provide evidence for them. GPX access is `unverified`.

The normalized source URL links to the OSM relation page. Results retain route
relation identity and an attribution string. Deduplication merges matching names
with the same rounded geometry fingerprint, or exact normalized names with start
points within 100 m and route lengths within 3%; alternate source URLs are
returned as aliases. Structured provenance includes the canonical route and each
merged source's provider, internal ID, provider route ID, source URL, attribution,
and source type.

## Geocoding and service etiquette

`near` accepts a place name or local `latitude,longitude` text. A valid coordinate
string is parsed locally and does not leave the machine. Place names use the
configured Nominatim endpoint with `format=jsonv2`, one result, a 30-day cache,
the configured HTTP User-Agent, and a minimum one-second provider interval. The
public Nominatim service has an acceptable-use policy; deployments should review
its current policy, avoid autocomplete or bulk geocoding, use caching, and
configure an alternative endpoint for larger workloads.

## Ranking

Scores combine only configured, available factors: route length fit, proximity,
activity suitability, loop match, provider-supplied popularity, and freshness
when a provider update timestamp exists. Missing popularity and freshness are
omitted from the score denominator and explicitly explained. OSM receives no
provider bonus. Ranking is a sorting aid and does not establish route quality or
current accessibility.


Undated search leaves closure risk unassessed. Dated search (`--date`) fetches the current/planned feeds once, assesses eligible candidates before applying the result limit, and includes closure status/confidence in each result. Known conflicts lower the closure factor; unknown coverage remains outside the denominator and is never a safety claim. Use `check-closures` for full per-event evidence before choosing a route. Scenic enrichment is optional and is not
performed by search; unavailable factors remain outside the score denominator.

## Saved community catalog and geographic criteria

`--provider local --source-type community` searches saved/imported routes with explicit community provenance. It uses the same typed discovery interface and retains original URLs, attribution and unknown metrics. It does not query a public community catalog or establish that someone rode the route. There is no fixture-directory fallback. Empty catalogs return import instructions. Use only GPX files you are entitled to retain.

`--bbox SOUTH,WEST,NORTH,EAST` controls the query extent, bounded within 100 km of its midpoint. Without `--near` its midpoint is the start-proximity origin; `--radius-km` still filters starts. `--shape loop` and `--shape point_to_point` require observable geometry evidence for the shape.
