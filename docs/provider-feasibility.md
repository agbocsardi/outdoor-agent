# Phase 0 provider feasibility

Checked: 2026-10-09 (UTC). Scope: public documentation and unauthenticated, read-only probes only. No provider account credentials were configured, no login was attempted, and no account writes were made. Capability states below use the project vocabulary: `supported`, `unsupported`, `unverified`, `restricted`, `degraded`.

## Decision

Do not make Ride with GPS the v1 community discovery source. Its official API is documented as an authenticated API, and its routes index is explicitly the authenticated user's routes. The public website has an Explore catalog, but the reviewed API documentation and `ride-cli` do not provide a supported public community-search operation. Public route detail and GPX export exist in the API contract but still require API authentication and respect visibility/permission rules; access to other people's GPX is not verified for this agent. Keep Ride with GPS as a later, opt-in adapter for a user's own/saved routes and possible COROS handoff, gated on credentialed probes and any needed entitlement checks.

NDW is a viable public source for Dutch road works and temporary traffic measures. Both current closure and planned works/event feeds were fetched successfully. The sample feeds are DATEX II v3 `SituationPublication`s, and contain ready GML lines as well as Alert-C/location-reference records. This supports a first road-works ingestion spike, but does not establish complete cycleway/trail coverage, correct resolution of every location reference, or an update SLA. Preserve unresolved records and mode/coverage uncertainty.

The documented Ride with GPS-to-COROS flow is user-mediated account linking and web/mobile actions. It does not establish a supported automated API write. When using the documented UI, Ride with GPS says a sent or pinned route appears in the COROS app; COROS's instructions still require the user to select the route and sync it to the physical device. An automated integration must not claim submission or watch delivery until separately verified.

## Capability matrix

| Provider capability | Recommended initial state | Evidence and boundary |
|---|---|---|
| RWGPS `discover_public` | `unsupported` | The official v1 OpenAPI describes `GET /api/v1/routes.json` as a paginated list for the authenticated user, with personal filters. It contains no documented public route-search operation. The web Explore catalog is not a documented API. Do not scrape it or advertise community discovery through `ride-cli`. |
| RWGPS `get_metadata` | `restricted` | `GET /api/v1/routes/{id}.json` is documented, requires API key/auth token, and returns 403 when a route is not visible to the user. An unauthenticated request returned 401. Authenticated public-route retrieval was not probed. |
| RWGPS `download_gpx` | `restricted` | `GET /api/v1/routes/{id}.{format}` documents GPX output and says visibility rules match route JSON: non-public routes need a privacy code unless owned by the authenticated user. API key/auth token are parameters. No authenticated or entitlement probe was made. Do not infer other-user export permission. |
| RWGPS `pin_route` | `unverified` | The API docs expose reading the authenticated user's pinned collection and describe “added” sync events when a user pins another user's route, but the reviewed OpenAPI has no documented pin-write operation. Official help describes UI pin/send. A write probe was intentionally not made. |
| RWGPS account access / `auth_required` | `supported` (required) | The API contract requires `api_key` and `auth_token` on the relevant routes operations; unauthenticated route-list and route-detail probes both returned HTTP 401 `Failed to authenticate the request`. No credentials were present. |
| RWGPS → COROS automated submission | `unverified` | Official help documents a user connecting accounts and using Send to COROS / pinned routes through Ride with GPS web/mobile UI. No supported API write operation was identified in the reviewed API spec, and no account write was tested. |
| COROS physical-watch sync by this CLI | `unsupported` | The official instructions place route selection and “sync with your device” in the COROS app. No direct watch API or automated watch-delivery evidence was found. Manual user handoff remains documented. |
| NDW live current closure feed fetch | `supported` | `https://opendata.ndw.nu/tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz` returned HTTP 200, 224,910 bytes, `Last-Modified: 2026-10-09 14:35:10 GMT`; body decoded as DATEX II v3. This establishes public retrieval at probe time only. |
| NDW planned works/events feed fetch | `supported` | `https://opendata.ndw.nu/planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz` returned HTTP 200, 17,155,023 bytes, `Last-Modified: 2026-10-09 14:30:10 GMT`; body decoded as DATEX II v3. Plan for bounded streaming/decompression and caching. |
| NDW geometry decoding | `degraded` | Sampled records include `loc:LinearLocation` / `loc:gmlLineString` with `srsName="WGS 84"`, and also `loc:AlertCMethod2Point`, `loc:AlertCMethod4Point`, and `loc:AlertCMethod4Linear`. Direct GML is available for many records; other references need the documented reference datasets/services and correct DATEX profile logic. No full decoder was implemented or validated. |
| NDW freshness/update cadence | `unverified` | Endpoint listing and feed `Last-Modified`/`publicationTime` fields are live and useful for freshness. In this snapshot, the current-closure publication time was 14:35Z and the planned feed publication time was 14:15Z (file modified 14:30Z). A one-time snapshot does not prove a reliable polling interval, latency bound, or horizon. |
| NDW reuse/license | `supported` with source checks | NDW's copyright page says CC0 1.0 applies unless a specific item says otherwise. The portal links the feeds as real-time open data. Preserve source/issuer/provenance and recheck item-specific notices; do not treat the website-wide default as proof that every embedded third-party item has identical terms. |
| OpenClaw skill format | `supported` (format documented) | Current docs specify a directory containing `SKILL.md`, YAML frontmatter with required `name` and one-line `description`, and optional gating such as required binaries. Docs support workspace `skills/` placement and `{baseDir}` references. The project's skill has not yet been loaded in an OpenClaw runtime. |

## Evidence and actual probes

### Ride with GPS and `ride-cli`

- Official API docs: <https://ridewithgps.com/api> (redirects to <https://ridewithgps.com/api/v1/doc>); machine-readable current contract fetched from <https://ridewithgps.com/api/v1/openapi.yaml> (HTTP 200, unauthenticated).
- Contract review: `GET /api/v1/routes.json` says “Returns a paginated list of routes for the authenticated user”; filters are name, visibility, distance, elevation, and archived status. `GET /api/v1/routes/{id}.json` returns route detail and documents 401/403 visibility behavior. `GET /api/v1/routes/{id}.{format}` documents GPX/TCX/FIT/KML export and visibility restrictions. Route and export operations list API key and auth token parameters.
- Read-only probes (2026-10-09):
  - `curl -L https://ridewithgps.com/api/v1/openapi.yaml` → HTTP 200, OpenAPI 3.0.1.
  - `curl -L 'https://ridewithgps.com/api/v1/routes.json?page=1&page_size=20'` → HTTP 401, `{"errors":["Failed to authenticate the request"]}`.
  - `curl -L 'https://ridewithgps.com/api/v1/routes/1.json?include_route=true'` → HTTP 401, same auth error. Route ID 1 was only a read request; no data was changed.
- Official source repo cloned read-only: <https://github.com/ridewithgps/ride-cli>, HEAD `6fa84e4790ce795aea07d88b14e4f786208fb705` (2026-03-02, “Rewrite README for clarity and usability”), MIT. README says `ride api` makes authenticated calls to the public API and gives examples for the current user and personal trips. Its direct tool example is `search_my_rides`; that is personal-history search, not public route discovery. Natural-language `ride`/`ride ask` depends on Claude Code and is outside this adapter's path; the direct API subcommand is separate.
- API documentation evidence for route-to-COROS: the reviewed OpenAPI has `GET /api/v1/collections/pinned.json` (authenticated user's pinned collection) and a sync feed that can report `added`/`removed` when the authenticated user pins another user's item. No pin-write operation appeared in the reviewed API document. Do not infer a write from a read endpoint or sync history.
- Official RWGPS help: <https://support.ridewithgps.com/hc/en-us/articles/27231998487963-COROS>. It describes connecting accounts through RWGPS Connected Services, sending routes via UI, and pinning routes; it says the route is then visible in the COROS app. It also instructs selecting the route and syncing it to the device in that app.
- Official COROS help: <https://support.coros.com/hc/en-us/articles/28320578199060-Connecting-Ride-with-GPS-to-COROS-Account>. It documents authorizing the connected account, sending a route from RWGPS, then finding it in COROS My Collection and selecting “Sync With Your Device.” These pages establish a manual supported handoff, not a CLI/API contract.

### NDW / Melvin

- Public feed directory: <https://opendata.ndw.nu/>. At probe time it listed `tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz` and `planningsfeed_wegwerkzaamheden_en_evenementen.xml.gz` under real-time data, with per-file “Last modified” and size fields.
- Both direct feed URLs returned HTTP 200 without credentials. The current closure sample had DATEX `mc:messageContainer`, `modelBaseVersion="3"`, `sit:SituationPublication`, `com:publicationTime`, source/issuer, validity times and `loc:locationReference`. The first sampled record used an indexed itinerary with `loc:gmlLineString srsName="WGS 84"`. The planning sample likewise used v3 and contained planned `sit:MaintenanceWorks` records.
- A read-only scan of feed XML type tokens found these location encodings in the current closure file: `LinearLocation`, `SingleRoadLinearLocation`, `PointLocation`, `AlertCMethod2Point`, `AlertCMethod4Point`, and `AlertCMethod4Linear`; in the planned works feed it found `LinearLocation`, `PointLocation`, `SingleRoadLinearLocation`, `AlertCMethod2Point`, `AlertCMethod4Point`, and `AlertCMethod4Linear`. The counts are sample-content counts, not a completeness or coverage measure.
- NDW's docs index identifies DATEX II v3 payload types including Situation; the live portal banner links to the current Verkeersdata NL DATEX II profile v3.6 for some datasets and states other v2.3 files remain unchanged. The exact closure feed sample observed here is v3. Do not assume every NDW file has migrated to the same profile.
- Location documentation starting points: <https://docs.ndw.nu/dataformaten/>; current profile: <https://mogin.ndw.nu/publicaties/Verkeersinformatie/Verkeersdata/vigerend/index.html>; Melvin/NDW guide: <https://docs.ndw.nu/handleidingen/Melvin/>; location-reference material is linked from the NDW docs index. Use the relevant v3 Situation and Alert-C reference specifications as the decoder contract, not only the XML sample.
- License: <https://www.ndw.nu/copyright> states the website's material is CC0 1.0 unless otherwise specified. NDW open data portal identifies the feeds as open data but the listing itself did not expose a per-file license field in the observed page. Keep exceptions and embedded source terms visible.
- Cadence remains a gap: the feed exposes `publicationTime` and HTTP `Last-Modified`; record both on every refresh and derive freshness from observed values. Probe repeatedly over a representative interval before choosing a poll interval or declaring an SLA.

### OpenClaw skill format

- Official docs: <https://docs.openclaw.ai/tools/creating-skills> and <https://docs.openclaw.ai/skills>.
- The current creating-skills guide says each skill is a directory containing `SKILL.md` with YAML frontmatter and Markdown instructions; `name` and `description` are required. It documents workspace `skills/` roots, a `requires.bins` gate for dependencies, and `{baseDir}` for paths relative to the skill. Current docs were fetched successfully on 2026-10-09. This validates the format guidance in the spec; it does not validate actual runtime loading on the target host.

## Remaining probes before production claims

1. With an explicitly configured user account, test only documented RWGPS authenticated reads: current user, owned/saved route metadata, and export for an owned route. Record plan/permission requirements and rate limits. Do not test public discovery through undocumented endpoints.
2. Ask whether RWGPS offers a documented API write for pin/send-to-COROS. If not, retain manual UI handoff. Any future write probe must be separately authorized and must be a user-approved route.
3. Validate full DATEX II v3 parsing against current and planned feed fixtures, including direct GML, each Alert-C reference variant, validity/recurrence, cancellation/revision semantics, and affected modes. Resolve references through the documented NDW/MOGIN method; preserve failures as unresolved coverage.
4. Repeat NDW fetches over time to establish observed refresh cadence, staleness behavior, and feed horizon. Clarify coverage for cycle paths, footpaths, municipal parks, and forestry closures; NDW road feeds do not establish these are covered.
5. Load the proposed OpenClaw skill in the target OpenClaw version and verify `outdoor-agent` invocation under the intended workspace and executable path.

### OSM discovery and geocoding follow-up

- Official Overpass documentation: <https://wiki.openstreetmap.org/wiki/Overpass_API>.
  A bounded read-only probe on 2026-10-09 used `https://overpass-api.de/api/interpreter`
  with query `[out:json][timeout:15];relation["type"="route"]["route"="hiking"](52.08,5.10,52.10,5.13);out tags 3;`.
  The server returned HTTP 406 and an Apache error page. No route data was obtained;
  runtime discovery remains **unverified**, not supported. This single failure does
  not prove that Overpass is unavailable generally. Retry with the documented request
  format and service policy in Phase 2, with a bounded query and identified client.
- <https://operations.osmfoundation.org/policies/nominatim/> was fetched successfully
  (HTTP 200). Public-service use requires an identified User-Agent/Referer, at most one
  request per second, caching and attribution; do not implement client-side autocomplete
  or systematic/bulk geocoding. Phase 1 performs no geocoding. A future adapter must let
  operators change endpoints and retain OSM/ODbL attribution.

### Current COROS platform boundary

The final review also fetched current official COROS support articles on 2026-10-09
through the public Help Center API (HTTP 200):

- [GPX route import troubleshooting](https://support.coros.com/hc/en-us/articles/360040243352-Troubleshooting-GPX-Route-Imports-for-Breadcrumb-Navigation)
  explicitly describes downloading a standard GPX, opening it in the COROS app,
  pressing Save, and then optionally syncing to a watch. This verifies the manual
  GPX handoff documentation, not this CLI's ability to perform it.
- [Partner API Access](https://support.coros.com/hc/en-us/articles/53181766856724-Partner-API-Access)
  (updated 2026-09-28) documents GPX import/export and `route/push`, with OAuth
  credentials issued after a registered-company/platform application, security checks
  and acceptance of API terms. This is a **restricted** official future integration
  option, not an undocumented endpoint. No partner credentials, exact API contract,
  authenticated route push, or downstream account/watch visibility were probed.
- [Build on COROS MCP](https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP)
  (updated 2026-09-10) documents self-service single-user OAuth access and workout-plan
  writes, but explicitly says **no GPX route import/export**. It is not a shortcut to
  the required route handoff and does not belong in the Phase 1 local CLI.

These newly reviewed partner/MCP docs do not change the initial decision: implement
local GPX handling now; keep account writes unavailable until a lawful documented
integration is configured, tested, and approved by the user. The advertised partner
route push must not be conflated with physical watch delivery.
