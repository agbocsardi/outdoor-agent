# outdoor-agent

`outdoor-agent` 0.3.0 is a local, JSON-first command-line tool for inspecting and saving GPX routes, searching saved community routes and mapped OpenStreetMap trails, assessing NDW and optional OSM closure evidence, enriching routes with opt-in Google Places data, and preparing a local GPX handoff for the COROS app. Python environments and commands use **uv**.

## Install

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then clone and install the locked project:

```bash
git clone https://github.com/agbocsardi/outdoor-agent.git
cd outdoor-agent
uv python install 3.12
uv sync --frozen --dev
uv run outdoor-agent --help
uv run outdoor-agent --version
uv run outdoor-agent doctor --json
uv run outdoor-agent providers --json
uv run outdoor-agent schema --name all --json
```

No `.env` file is loaded automatically. Set environment variables in the process manager or shell, or use `~/.config/outdoor-agent/config.toml` with restrictive permissions. `--config ./config.toml` selects a different TOML file.

## Discover and save routes

Search returns mapped OSM route relations as `official_trail`, with OSM attribution and relation links. OSM route relations are not community reviews or proof that a route was ridden. Public community-route search is not available: `--source-type community` with public providers returns the typed `COMMUNITY_DISCOVERY_UNAVAILABLE` error (exit 5). The default `all` search can return OSM results and reports this community-catalog gap as a partial result. Results with incomplete member geometry, stale cache data, or Overpass truncation also carry warnings.

```bash
uv run outdoor-agent search \
  --near "Utrecht, Netherlands" \
  --radius-km 30 \
  --activity gravel \
  --distance-km 20:35 \
  --loop preferred \
  --date 2026-10-11 \
  --limit 5 \
  --json
```

`--near` accepts a place name or local `latitude,longitude`. Place names go to the configured Nominatim service and use a 30-day cache. OSM search uses a radius-bounded Overpass request (up to 100 km and 200 route relations), cached according to `discovery_cache_ttl_seconds`. Activity tags narrow the request. A radius crossing the antimeridian is rejected because one Overpass bbox cannot represent it. Cached results report freshness; stale cache and partial Overpass responses never appear as clean fresh results. See [discovery details](docs/discovery.md) for the probe evidence, ODbL attribution, geometry reconstruction, ranking and limits.

Use `--bbox SOUTH,WEST,NORTH,EAST` for an explicit bounded query extent; `--near` then becomes optional. Without an origin, the bbox midpoint is the origin. `--radius-km` still filters route starts relative to that origin. `--shape point_to_point` requires a known non-loop; `--shape loop` requires a known loop. With `--date`, candidates receive closure assessments before ranking and limiting; unknown coverage is explicitly excluded from the closure score.

Searches save normalized route metadata locally. List and retrieve saved routes without another provider request:

```bash
uv run outdoor-agent routes --limit 100 --json
uv run outdoor-agent get --route-id osm:relation:93841 --json
uv run outdoor-agent get --route-id osm:relation:93841 --refresh --json
```

To retrieve Ride with GPS metadata, configure `RIDEWITHGPS_API_KEY` and `RIDEWITHGPS_AUTH_TOKEN` through secure environment injection or private TOML. These credentials are sent in provider headers. The adapter supports authenticated route metadata and GPX export only when the provider permits the requested route. It does not provide public community search, route pinning, or account writes. `doctor --probe` performs an explicit read-only authentication request; ordinary `doctor` does not contact providers. Credential presence is not proof that the credentials work.

## Inspect or import GPX

Inspection is local and read-only. Import preserves the exact source bytes, stores a normalized route and records the supplied provenance. A GPX file alone does not establish who created or rode the route.

```bash
uv run outdoor-agent inspect ./ride.gpx --activity gravel --json
uv run outdoor-agent import-gpx ./ride.gpx \
  --activity gravel \
  --source-url https://ridewithgps.com/routes/12345 \
  --source-type community \
  --attribution "Original route creator" \
  --json
```

Use the original URL and attribution when known. Import does not fetch or validate the remote page or establish export rights. URL validation only accepts supported HTTPS source hosts and rejects credentials, query strings, fragments, private endpoints and nonstandard ports. Unknown provenance remains `unknown`; ratings and popularity are never invented.

Search your imported catalog without contacting a route-sharing account:

```bash
uv run outdoor-agent search --provider local --source-type community \
  --near "52.09,5.12" --radius-km 30 --activity gravel \
  --distance-km 20:35 --date 2026-10-11 --limit 5 --json
```

This searches only saved routes with explicitly supplied community provenance. It does not crawl a public catalog or independently establish authorship or a prior ride. An empty catalog returns setup instructions. Coordinate origins and undated local searches make no external calls; dated searches fetch closure sources unless offline or an explicit fixture is selected.

GPX tracks, routes and disconnected segments are preserved separately. Distance uses WGS84 geodesics and excludes gaps. Invalid coordinates, unsafe XML, oversized files and excessive point counts are rejected. Geometry is not silently joined, repaired or simplified.

## Check closures and recheck saved routes

The closure check reads both current and planned public NDW feeds unless `--feed-file` is supplied. The parser decodes supported direct WGS84 GML lines. Alert-C and other unresolved location references remain unknown. NDW primarily covers road traffic measures; parks, trails, paths and forestry closures have incomplete coverage. The observed future horizon may be unknown or end before the requested date.

```bash
uv run outdoor-agent check-closures \
  --route-id osm:relation:93841 \
  --date 2026-10-11 \
  --activity hiking \
  --json

# Explicit local fixture mode; output is marked fixture_mode.
uv run outdoor-agent check-closures \
  --gpx tests/fixtures/example.gpx \
  --date 2026-10-11 \
  --activity gravel \
  --feed-file tests/fixtures/ndw_closures.xml \
  --json

# Recheck selected saved routes, or omit --route-id to check every saved route.
uv run outdoor-agent recheck \
  --route-id osm:relation:93841 \
  --date 2026-10-11 \
  --json
```

Dates are interpreted as the local calendar day in `default_timezone` (default `Europe/Amsterdam`), including 23- or 25-hour DST days. A datetime denotes an instant. Naive local datetimes must be unambiguous and existent; ambiguous or nonexistent DST times are rejected and require an explicit UTC offset. Date precision and timezone are retained in results.

Add `--osm-restrictions` to `check-closures` or `recheck` to query bounded OSM access/construction tags near the selected route. These tags have unknown temporal validity and conditional rules remain uncertain; they supplement road feeds and never prove full trail coverage. Reports expose each source's last successful refresh, publication age, cache state, ETag and Last-Modified.

Possible statuses include `known_conflict`, `possible_conflict`, `no_known_conflict`, `coverage_unknown`, and `check_failed`. A feed outage or unresolved location cannot be treated as safe, and `no_known_conflict` does not guarantee a route is open. Partial results are returned with warnings; add `--strict` to exit 6 after emitting them. Each check stores a stable comparison against the previous finding set. SQLite schema version 2 migrates older state transactionally; back up the SQLite database together with its GPX artifacts.

## Optional Google Places enrichment

Google requests are opt-in: set `google_enabled = true` and supply `GOOGLE_MAPS_API_KEY` independently. The default switch is off. Keep a small explicit `max_google_requests_per_run`; zero prevents requests, and a missing key is reported as an error. Offline mode skips the provider and returns a partial result. The adapter samples at most 20 route points, searches within 500 m, requests at most 10 places per sample, and bounds response size. Only the sampled point centers are sent, not the GPX or full route. The budget counts HTTP sends; provider retries are disabled. Google responses are not cached or persisted. Review Google Maps Platform terms for your intended use and display its attribution.

```bash
uv run outdoor-agent places \
  --route-id osm:relation:93841 \
  --category viewpoint \
  --provider google \
  --json
```

Categories include `viewpoint`, `attraction`, `trailhead`, `park`, `campground`, and `picnic`; viewpoint and trailhead use broader Google type proxies and are labeled in warnings.

## Export or prepare a COROS handoff

```bash
uv run outdoor-agent export \
  --route-id local:YOUR_ROUTE_ID \
  --format gpx \
  --output ./exports/route.gpx \
  --dry-run \
  --json

# After reviewing the route/path, commit that local export.
uv run outdoor-agent export --route-id local:YOUR_ROUTE_ID \
  --output ./exports/route.gpx --confirm --json

uv run outdoor-agent deliver \
  --route-id local:YOUR_ROUTE_ID \
  --target coros \
  --account "my COROS account" \
  --dry-run \
  --json

# After reviewing the plan, --confirm prepares a local GPX for manual app import.
uv run outdoor-agent deliver \
  --route-id local:YOUR_ROUTE_ID \
  --target coros \
  --account "my COROS account" \
  --confirm \
  --json
```

Export now defaults to a read-only plan. Use `--confirm` to write the reviewed path; `--confirm --overwrite` explicitly permits replacing different bytes. Export preserves original imported GPX bytes when available. An OSM route export encodes the currently mapped geometry into a new GPX; it is not an original provider GPX and disconnected segments stay separate. Ride with GPS GPX bytes are exported only through the authenticated provider endpoint when permitted. The export command will not overwrite an existing file unless `--overwrite` is supplied.

Delivery is a local manual handoff. The default and `--dry-run` report a plan; `--confirm` writes a local GPX and records local preparation. The account option is only a human-readable label. No Ride with GPS pin, COROS account submission, or watch sync occurs. Results report `watch_sync_unverified`; follow the instructions in the COROS app and verify the route on the device yourself.

## Configuration and network behavior

Configuration precedence is environment variables, TOML, then defaults. `.env.example` lists supported placeholders, but the application does not load `.env` automatically. Keep private TOML and credential injection files restricted to the service user. Configure trusted HTTPS provider endpoints without query parameters. Literal private/loopback IPs and local hostnames are rejected. Redirects are not followed. Requests have bounded timeouts and payload sizes, provider-specific throttling, and up to three attempts for transient HTTP failures. On Linux, file locks coordinate throttling across processes. HTTP caches are local, private TTL files under `cache_dir`; stale entries used in offline mode are explicitly marked. OSM discovery uses `discovery_cache_ttl_seconds`; NDW uses `ndw_cache_ttl_seconds` and reports data older than `ndw_max_age_seconds` as stale. Nominatim uses a 30-day cache. Google responses and authenticated Ride with GPS responses are not cached.

```toml
[outdoor_agent]
data_dir = "/srv/outdoor-agent/state"
cache_dir = "/srv/outdoor-agent/cache"
default_timezone = "Europe/Amsterdam"
default_region = "NL"
offline = false
disabled_providers = ["ridewithgps"]
google_enabled = false
max_google_requests_per_run = 0
discovery_cache_ttl_seconds = 86400
ndw_cache_ttl_seconds = 600
ndw_max_age_seconds = 3600
closure_buffer_m = 20
# Optional secrets; keep this TOML file private, or inject these variables instead.
# ride_api_key = "..."
# ride_auth_token = "..."
```

OSM data is © OpenStreetMap contributors and available under ODbL; retain attribution and review ODbL obligations for redistributed databases or derived databases. Google content has separate Google Maps Platform terms and is deliberately not retained. The command sends only the location/search data needed by the selected external provider; inspection and local storage remain local.

## JSON and exit codes

Information commands support `--json` and emit one versioned response object on stdout. Diagnostics go to stderr. `--strict` on partial-capable commands emits the result and then exits 6. Null means unknown, not zero.

| Exit | Meaning |
|---|---|
| 0 | Complete operation |
| 2 | Invalid arguments, data, or configuration |
| 3 | Authentication or permission required |
| 4 | Provider unavailable, rate-limited, or request budget exhausted |
| 5 | Provider, activity, or capability unsupported/disabled |
| 6 | Strict mode found partial or insufficient coverage |
| 7 | Storage or unexpected local failure |

`providers --json` reports capability evidence separately from implemented behavior. See the dated [provider feasibility notes](docs/provider-feasibility.md); provider documentation is not evidence that every route is accessible.

## OpenClaw

Copy [skills/openclaw/outdoor-routes](skills/openclaw/outdoor-routes) into `<OpenClaw workspace>/skills/outdoor-routes`. A symlink resolving outside configured skill roots requires an explicitly trusted `skills.load.allowSymlinkTargets` target. Set the project root once in the skill instructions (for example `/srv/outdoor-agent`); commands use `uv run --project /srv/outdoor-agent outdoor-agent ...` from any working directory. The skill confirms delivery preparation, honors requested imports and exports, distinguishes official mapped trails from community routes, and treats unknown closure coverage conservatively. The file follows the documented OpenClaw skill layout; loading it on the target host remains an operator integration check.

## Development and operations

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv lock --check
```

CI runs offline without paid or credentialed provider calls. Run the tool as an unprivileged user, restrict configuration and state directory permissions, and back up the SQLite database and GPX artifacts together. There is no listener, telemetry service, or background daemon.


Public integration tests require explicit opt-in and perform only small Nominatim/Overpass reads:

```bash
OUTDOOR_AGENT_LIVE_TESTS=1 uv run pytest -m live
```

See [the completed CLI smoke demo](docs/cli-demo.md) for dated live evidence, and
[deployment and prompt recipes](docs/operations.md) for an SSH/OpenClaw installation.

The [spec acceptance matrix](docs/spec-acceptance.md) records implemented requirements and external verification gates. Provider `raw_metadata` is scrubbed of credentials/contact details, stripped of URL credentials/query strings, and bounded to 32 fields, 512 characters per value and 4 KiB total. Source attribution and original GPX bytes remain intact.
