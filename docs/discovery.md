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

On 2026-10-10, a single read-only `ride api get /api/v1/routes.json` probe of the
authenticated account returned an empty page: `record_count` 0, `page_count` 0,
`page_size` 20 and `next_page_url` null. No token, credential or route content was
recorded. This is the account's saved-route state on that date, not a claim about
any other account.

This verifies that these public read endpoints responded to these example
requests on that date. It does not establish uptime, complete geographic
coverage, route access conditions, public community search, GPX download rights,
or a recommendation/popularity signal. Mapped geometry, names, route tags and
endpoint behavior can change after the probe.

## Authenticated Ride with GPS saved routes

The documented `GET /api/v1/routes.json` (`listRoutes`) endpoint returns a
paginated list **for the authenticated user**, not a public catalog. Public
community discovery remains unavailable and `discover_public` stays `unsupported`.
The supported seeds are an explicit public route URL and a local GPX import.

`search --provider ridewithgps` lists the account's own saved routes. It requests
`page` and `page_size` (20-200) through the authenticated transport, then follows
`meta.pagination.next_page_url` by extracting only its page number and re-issuing a
validated `/api/v1/routes.json` request; an arbitrary URL is never fetched. It stops
at a null `next_page_url`, an empty page, or the requested matched-result limit, and
is hard-bounded to 20 pages and 500 scanned records. Activity and geographic (bbox)
filters are applied client-side while paging, so an in-area match beyond the
matched-result cap is not lost; the page/record scan budget is independent of that
cap, and reaching a scan bound adds the truncation warning to the returned routes. A malformed or missing pagination object is a typed
`PROVIDER_SCHEMA_INVALID` error. An account with zero saved routes returns
`RIDEWITHGPS_NO_SAVED_ROUTES` (exit 5) with a suggestion to save routes, fetch one
explicit public route URL, or import a local GPX; it is never an empty success.

Because this source is account-scoped, a selected saved-only provider adds an
explicit warning that the results are the authenticated user's own saved routes, not
a public community catalog. `--source-type community` still requires
`--provider local`; Ride with GPS saved routes are not folded into that gate. The
routes keep their existing `community` source label and the warning makes the scope
explicit under `--source-type all`.

A known route can also be addressed by its public URL. `canonical_route_reference`
accepts `https://ridewithgps.com/routes/<id>` (and the `www.` host) with no query,
fragment, credentials or port, and maps it to `ridewithgps:<id>`; every other value
is returned unchanged. `get`, `export` and `deliver` normalize this reference before
store lookup or provider dispatch, so `get --route-id
https://ridewithgps.com/routes/12345` saves metadata and geometry to the RouteStore,
`search --provider local` can find the saved record, and `export --confirm` writes the
original provider GPX bytes from the same cached artifact with a matching SHA-256.

## Experimental public Ride with GPS Explore search

`search --provider ridewithgps_public` is an opt-in, read-only public community search
over the undocumented Ride with GPS website Explore endpoint
(`GET https://ridewithgps.com/explore.json`). It is not the documented API and is not
licensed; permission, stability and pagination semantics are unverified, so it is
declared `degraded` and every result carries an experimental warning. It uses no
credentials, cookies or `ride` CLI; `auth_required=false` applies to search only.

The adapter sends the verified plural selectors (`activity_types`, `track_types`,
`models`) and meter `length_min`/`length_max`, and never the ignored singulars. Because
the endpoint expands the requested bounding box, user activity, distance, loop and
start-radius filters are applied locally before the result cap. Pagination follows only
a validated same-origin `/explore.json` `next_page_url` (the live pointer is relative
and carries `offset`/`next_assets`/`sort_by`), with cycle protection and hard
page/record/byte/wall-time bounds; reaching a bound or an invalid pointer is loud, never
a silent complete.

Explore results retain the source URL, author attribution, community provenance and
`unpaved_pct`, but carry only a `simplified_polyline`. That preview is never treated as
navigation-complete geometry: `geometry` stays null and closure assessment is not
derived from it. Real geometry, permitted original-byte GPX export and closure checks
go through the existing authenticated documented API, which keeps the canonical
`ridewithgps:<id>` id and `provider="ridewithgps"` so detail/export and deduplication
resolve. A dated live acceptance verifies a real 20-35 km gravel loop within 20 km of
Den Bosch through this path; RWGPS activity/surface labels remain imperfect suitability
evidence, not a ride-ready recommendation.

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
across a gap. An invalid coordinate rejects the entire member way rather than
connecting its remaining points. Missing or rejected members mark geometry
incomplete; disconnected geometry also leaves route length and loop shape unknown.
Only complete geometry receives a WGS84 geodesic route length. Elevation,
popularity, rating and review count remain unknown because this query does not
provide evidence for them. GPX access is `unverified`.

Version 0.3.1 marks safely normalized OSM records with `geometry_normalizer=osm-v2`.
Older saved OSM records retain identity and provenance but their geometry and
derived measurements are quarantined. Export and handoff require an explicit
`get --route-id osm:relation:ID --refresh` first; old cached exports cannot bypass
this check. Local original GPX imports are unaffected.

The result's `coverage` object counts provider candidates, source exclusions,
unique candidates, missing starts, radius/distance/shape exclusions, incomplete
geometry, eligible and returned routes, and provider truncation. Exclusion
counters can overlap. They explain the available data, not the number of routes
that exist in the area. A capped or disconnected candidate set producing zero
matches is a coverage/reconstruction limitation. Import a permitted community GPX
and search the saved catalog when the public mapped-route source cannot satisfy
the requested distance and loop criteria.

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

`--provider local --source-type community` searches saved/imported routes with explicit community provenance. It uses the same typed discovery interface and retains original URLs, attribution and unknown metrics. It does not query a public community catalog or establish that someone rode the route. There is no fixture-directory fallback. Empty catalogs return import instructions. Use only GPX files you are entitled to retain. Authenticated Ride with GPS saved routes are a separate account-scoped provider and do not satisfy this community gate.

`--bbox SOUTH,WEST,NORTH,EAST` controls the query extent, bounded within 100 km of its midpoint. Without `--near` its midpoint is the start-proximity origin; `--radius-km` still filters starts. `--shape loop` and `--shape point_to_point` require observable geometry evidence for the shape.
