---
name: outdoor-routes
description: Find existing saved community routes and mapped trails, inspect and save GPX, assess closure evidence, enrich with opted-in places, and prepare a manual COROS GPX handoff.
metadata: {"openclaw":{"requires":{"bins":["uv"]}}}
---

# outdoor-routes

Use the installed outdoor-agent project as the only route-data tool. Set its fixed
project root once in this skill's command prefix. The examples use
`/srv/outdoor-agent`; replace that path with the actual installation root. From any
working directory, run commands as:

```bash
uv run --project /srv/outdoor-agent outdoor-agent <command> <arguments> --json
```

Keep `--project` in the command prefix once. Do not open another model session or
use `ride ask`. Read one JSON response object from stdout; diagnostics are on stderr.

## Discover and inspect routes

Check current implementation and provider evidence when needed:

```bash
uv run --project /srv/outdoor-agent outdoor-agent doctor --json
uv run --project /srv/outdoor-agent outdoor-agent providers --json
```

Search mapped routes with the user's location, activity, distance and loop
preferences:

```bash
uv run --project /srv/outdoor-agent outdoor-agent search \
  --near "Utrecht, Netherlands" --radius-km 30 --activity gravel \
  --distance-km 20:35 --loop preferred --date 2026-10-11 --limit 5 --json
```

Search results from OSM are labeled `official_trail` and linked to their OSM route
relations. This means mapped route relation; it does not mean community review,
proof of a prior ride, access permission, or current trail status. No public
community-route search provider is configured. If the user asks for community
routes only from public providers, explain that search returns a typed unavailable error; do not present
OSM or fixtures as community alternatives. In mixed/default search, describe OSM
results as mapped official-trail records and state the community-catalog warning.

Respect result `status`, `provider_status`, warnings, score explanations, geometry
quality, provenance, cache freshness and route attribution. A partial result is not
a complete search. Null distance, elevation, popularity and rating mean unknown.
OSM route relations do not provide popularity/review evidence. Search saves
normalized route records locally; use the returned route ID for follow-up commands.
Use `get` to read or explicitly refresh one saved route, and `routes` to list saved
routes:

```bash
uv run --project /srv/outdoor-agent outdoor-agent get --route-id osm:relation:93841 --json
uv run --project /srv/outdoor-agent outdoor-agent routes --limit 100 --json
```

Inspect local GPX without storing it, or import it when the user wants it saved:

```bash
uv run --project /srv/outdoor-agent outdoor-agent inspect "/safe/path/ride.gpx" --activity gravel --json
uv run --project /srv/outdoor-agent outdoor-agent import-gpx "/safe/path/ride.gpx" \
  --activity gravel --source-url "https://example.org/route" \
  --source-type community --attribution "Verified route creator" --json
```

Search entitled GPX routes saved with community provenance using `search --provider local --source-type community --near "52.09,5.12" --activity gravel --date 2026-10-11 --json`. This is a private saved catalog, not public discovery or independent proof of authorship/riding. Dated search assesses closures before ranking; inspect the full evidence for final candidates.

Only supply source type and attribution when the source supports them. GPX alone
does not prove community authorship. Original imported bytes are preserved locally;
do not share personal track details in chat or logs without the user's request.

## Check closures and recheck

Use the user's planned date and activity. For live checks, omit `--feed-file`:

```bash
uv run --project /srv/outdoor-agent outdoor-agent check-closures \
  --route-id osm:relation:93841 --date 2026-10-11 --activity hiking --json
```

Optionally use `--osm-restrictions` to supplement NDW with mapped access/construction tags; their timing and completeness remain unknown.

Use `--feed-file` only when the user explicitly asks to assess a supplied local
feed or fixture; that run is labeled `fixture_mode` and is not live safety evidence.
The repository fixture example is:

```bash
uv run --project /srv/outdoor-agent outdoor-agent check-closures \
  --gpx /srv/outdoor-agent/tests/fixtures/example.gpx \
  --date 2026-10-11 --activity gravel \
  --feed-file /srv/outdoor-agent/tests/fixtures/ndw_closures.xml --json
```

Recheck one or more saved route IDs, or omit `--route-id` to check all saved routes:

```bash
uv run --project /srv/outdoor-agent outdoor-agent recheck \
  --route-id osm:relation:93841 --date 2026-10-11 --json
```

NDW currently provides road-traffic measures. The parser reads direct WGS84 GML
lines; Alert-C and unsupported location references remain unresolved. Trail, park,
municipal path and forestry coverage is incomplete. The observed future horizon can
be unknown or shorter than the requested date. Feed outages, stale data, incomplete
coverage and unresolved geometry are warnings or partial/error states, never proof
that a route is safe. `no_known_conflict` does not mean guaranteed open. Date-only
inputs mean the whole calendar day in the configured timezone (default
Europe/Amsterdam). Date precision is preserved, including DST-length days.
Ambiguous or nonexistent naive datetimes are rejected; use an explicit UTC offset.

## Optional places enrichment

Only call Google Places when the user requests places and the operator has enabled
Google in configuration and provided `GOOGLE_MAPS_API_KEY`. A missing key or zero
request budget prevents the call. Offline mode returns a partial result indicating
that enrichment was skipped. Explain that only up to 20 sampled point centers are
sent, each search is bounded to a 500 m radius and 10 results, and the GPX and full
route are not sent. Responses are not cached or persisted. Show Google attribution
and retain provider links; viewpoint and trailhead categories use broader type
proxies noted in warnings.

```bash
uv run --project /srv/outdoor-agent outdoor-agent places \
  --route-id osm:relation:93841 --category viewpoint --provider google --json
```

## Export and COROS manual handoff

Export defaults to a read-only plan. Show the route, local path and effects, obtain approval, then use `--confirm`:

```bash
uv run --project /srv/outdoor-agent outdoor-agent export --route-id local:YOUR_ROUTE_ID \
  --output /safe/path/route.gpx --dry-run --json
# After approval:
uv run --project /srv/outdoor-agent outdoor-agent export --route-id local:YOUR_ROUTE_ID \
  --output /safe/path/route.gpx --confirm --json
```

Export GPX only when requested. Existing output files are protected; use
`--overwrite` only when the user has requested replacement. Original imported GPX
bytes are preserved when available. An OSM export encodes its current mapped
geometry into a new GPX, not an original provider GPX; gaps remain separate.
Ride with GPS GPX retrieval requires configured credentials and provider permission
for that route.

Delivery defaults to a read-only plan. Show the route, destination label, output
path and side effects. `--account` is only a label. Get explicit user approval for
local preparation before using `--confirm`:

```bash
uv run --project /srv/outdoor-agent outdoor-agent deliver \
  --route-id osm:relation:93841 --target coros --account "my COROS account" \
  --dry-run --json

# Only after approval for this route and local output:
uv run --project /srv/outdoor-agent outdoor-agent deliver \
  --route-id osm:relation:93841 --target coros --account "my COROS account" \
  --confirm --json
```

`--confirm` writes a local GPX and records local preparation. It does not submit to
Ride with GPS or COROS, authenticate an account, or verify watch synchronization.
Report `watch_sync_unverified` accurately. The user must import the GPX in the COROS
app and verify route arrival on the physical watch.

## Provider credentials and privacy

Never ask the user to paste credentials into chat. Ride with GPS reads use
`RIDEWITHGPS_API_KEY` and `RIDEWITHGPS_AUTH_TOKEN` from secure environment injection
or private TOML and send them only in provider headers. `doctor --probe` is an
explicit read-only authentication request; ordinary `doctor` is local. Authenticated
metadata or permitted GPX export does not establish public route search or pin/write
capability. Google calls require separate explicit operator opt-in and an API key.

The CLI does not load `.env` automatically. Keep configuration, SQLite state, HTTP
cache and original GPX files private. Provider requests use HTTPS public endpoints,
bounded payloads, throttling, retries and no redirects. OSM data requires attribution
and ODbL compliance. Google Maps content has separate terms and is not persisted.
The tool has no listener or telemetry.

Official OpenClaw skill conventions: https://docs.openclaw.ai/tools/creating-skills
and https://docs.openclaw.ai/skills. Validate skill loading on the target host.
