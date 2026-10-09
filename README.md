# outdoor-agent

Local outdoor-route tools for OpenClaw and Ubuntu, built with Python 3.12+ and **uv**.
This initial milestone implements **Phase 0 feasibility and Phase 1**, not the full v1 workflow.
It makes no provider requests or account writes at runtime.

Working commands: `inspect`, `import-gpx`, `providers`, `doctor`, `schema`, and `--version`.
`search`, `check-closures`, `places`, provider `export`, and `deliver` are explicit future-phase
command targets: they return `CAPABILITY_NOT_IMPLEMENTED` with exit code 5. Neither
`--dry-run` nor `--confirm` enables an unverified delivery operation.

## Install and try it

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/agbocsardi/outdoor-agent.git
cd outdoor-agent
uv python install 3.12
uv sync --frozen --dev
uv run outdoor-agent --help
uv run outdoor-agent doctor --json
uv run outdoor-agent providers --json
uv run outdoor-agent inspect tests/fixtures/example.gpx --activity gravel --json
uv run outdoor-agent import-gpx tests/fixtures/example.gpx --activity gravel --json
uv run outdoor-agent schema --name inspection --json
```

The example GPX is **synthetic test data**, not a recommendation or a verified real route.
For your own legitimately obtained route:

```bash
uv run outdoor-agent import-gpx ./ride.gpx \
  --activity gravel \
  --source-url https://ridewithgps.com/routes/12345 \
  --source-type community \
  --attribution 'Original route creator' \
  --json
```

Substitute your actual original URL and author. Import does not fetch the URL, verify
route ownership/entitlement, or imply permission to republish. Source URLs are HTTPS
and limited to Ride with GPS, Wikiloc, AllTrails, Strava and OpenStreetMap hosts;
credentials, query strings, fragments and nonstandard ports are rejected. An embedded
safe GPX metadata link is retained when no URL is explicitly supplied. Other embedded
links remain in the original GPX bytes with a warning. Creator names are retained;
email addresses and arbitrary extensions are not copied into normalized metadata.
A local GPX has `source_type: unknown` unless the user supplies provenance. It never
acquires fabricated ratings, reviews or popularity.

## GPX and local state

Inspection is read-only and does not create state directories. Import stores the exact
original bytes under `$XDG_DATA_HOME/outdoor-agent/gpx/<sha256>.gpx` and a versioned
SQLite record in `state.sqlite3`. If XDG variables are unset, data lives under
`~/.local/share/outdoor-agent`; the future HTTP cache defaults to
`~/.cache/outdoor-agent`. Cache configuration is available now; no HTTP cache is needed
in Phase 1.

Stable IDs use `local:<sha256-of-original-bytes>`. Repeat imports reuse the existing
record, timestamp and provenance; first import wins. Different byte representations
remain different imports. Geometry deduplication and provenance merging belong to
Phase 2. Imported GPX artifacts and newly created state files are private (0600),
with new app directories 0700. Existing parent-directory permissions remain under
operator control. Altered/missing stored artifacts produce `CACHE_CORRUPT`.

Track segments and routes remain separate. Distance uses the WGS84 ellipsoid and
excludes gaps; points are never silently connected or repaired. Coordinates retain
EPSG:4326 precision, and multiple segments become a MultiLineString. Isolated points
remain in counts/bounds and the unchanged artifact but cannot form line geometry.
Waypoint-only files have null route length and no line geometry. Elevation gain is
null if track/route elevation samples are incomplete; it sums positive differences
within each segment otherwise. Loop detection uses an explicit 20 m endpoint
proximity heuristic only for a single segment. It does not verify navigability. When a GPX contains both tracks and routes, totals
include all supplied segments and may count alternative representations twice; a
warning makes that ambiguity explicit.

Inputs are limited to 16 MiB and 100,000 GPX points. Invalid/empty GPX, out-of-range
or nonfinite coordinates/elevations, DTDs and XML entities are rejected. UTF-8 XML
(including a BOM) is supported. No automatic GPX rewriting or simplification occurs.

## Configuration

Use `~/.config/outdoor-agent/config.toml` (or `$XDG_CONFIG_HOME`), or pass a root option:

```bash
uv run outdoor-agent --config ./config.toml doctor --json
```

```toml
[outdoor_agent]
data_dir = "/srv/outdoor-agent/state"
cache_dir = "/srv/outdoor-agent/cache"
default_timezone = "Europe/Amsterdam"
default_region = "NL"
http_timeout_seconds = 20
offline = true
google_enabled = false
max_google_requests_per_run = 20
```

Environment overrides TOML, which overrides defaults. [.env.example](.env.example)
contains placeholders; `.env` is not automatically loaded. Future adapters will use
`RIDE_CONFIG_DIR` and separately injected `GOOGLE_MAPS_API_KEY`, never OpenClaw's
internal secrets. `doctor` reports credential configuration **presence**, never values,
and does not authenticate or probe a provider. Timeout, offline and Google budget
settings are validated now for future adapters; no Google calls occur even when the
flag is enabled in Phase 1. Unknown TOML fields fail rather than being ignored.

Global `--json`, `--quiet`, `--verbose`, `--config` and `--version` precede commands;
`--json` is also accepted after each command. Quiet suppresses optional human warnings;
JSON retains all warnings. Verbose adds only non-sensitive local diagnostics to stderr.
Help and plain version output are human-readable.

## Machine contracts

Every information command with `--json` returns one object on stdout, with
`schema_version: "1.0"`, `status`, request ID and UTC generation time. Logs go to stderr.
Bad input, parser errors and provider unavailability also return the same envelope.
Published schemas are in [schemas/v1](schemas/v1); `schema --json` exports matching
schemas. Validate the envelope with `response.json`, and its data with the command's
`inspection.json`/`import_result.json` or each provider with `provider.json`.
Null measurements mean unknown. `partial` is supported by the model and contract
fixture for later adapters; Phase 1 does not perform live searches or closure checks.

| Exit | Meaning |
|---|---|
| 0 | Complete local operation |
| 2 | Bad arguments, GPX, URL or configuration |
| 3 | Credentials/permissions (reserved for adapters) |
| 4 | Provider unavailable/rate limit (reserved) |
| 5 | Capability not implemented/supported |
| 6 | Strict insufficient/partial coverage (reserved) |
| 7 | Storage/internal failure |

Capability evidence and an installed adapter are separate fields. A supported NDW
feed fetch does **not** mean this release can assess closures. See the dated
[provider investigation](docs/provider-feasibility.md): the documented Ride with GPS
API scopes listing to an authenticated user's routes; public community search is not
established. OSM route relations are the planned Phase 2 official-trail source.
No known closure would never guarantee an open route; Phase 1 cannot check closures.

## OpenClaw

Copy or symlink [skills/openclaw/outdoor-routes](skills/openclaw/outdoor-routes) into
`<OpenClaw workspace>/skills/outdoor-routes`. Set the actual project root in the
skill's command instructions (example `/srv/outdoor-agent`). `uv run --project`
works without relying on the agent's current directory. The skeleton only invokes
working local commands and explicitly describes future phases and confirmation rules.
The current skill format was checked against official documentation; loading it on
the target OpenClaw host remains an operator integration check.

## Development and operations

```bash
uv sync --dev
uv run pytest --cov=outdoor_agent.geo --cov=outdoor_agent.models --cov-report=term-missing
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv lock --check
```

CI runs the same checks on Ubuntu with no API keys. Baseline tests deny socket
connections, use isolated XDG directories and synthetic fixtures, and never depend on
live provider availability. Click/Typer versions are bounded to retain the public Click
extension interface used for structured parser errors.

Run as an unprivileged SSH/service user, keep configuration/state private, and back up
SQLite **and** original GPX artifacts together. Take a stopped-process copy or use
SQLite's backup API for consistent snapshots. Do not place state in a world-writable
shared directory. No listener, telemetry, daemon or second LLM is required. Inspection
returns intentionally requested track details to stdout; avoid sending that output to
shared logs. Later geocoding/search will send locations to the selected provider and
closure fetching will request public feeds; those calls are not implemented here.

The next increments are live OSM discovery, credentialed lawful Ride with GPS reads,
NDW date/mode-aware closure analysis, then approved export and COROS handoff. The full
[specification](spec.md) remains the target; its final acceptance demo is not claimed
complete by this milestone.
