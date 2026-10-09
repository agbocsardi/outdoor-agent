# outdoor-agent — Product & Engineering Specification

**Status:** Implementation-ready specification, v1.0  
**Target repository:** `agbocsardi/outdoor-agent`  
**Primary runtime:** Ubuntu Linux homelab  
**Primary consumer:** OpenClaw agent via local CLI  
**Python tooling:** **uv exclusively**  
**Primary device:** COROS PACE 4  
**Initial geographic focus:** Netherlands, extensible worldwide  
**Last updated:** 2026-10-09

> This is a specification, not a claim that any integration is already built. External provider capabilities must be probed and verified before marking them supported. The main value proposition is **finding existing community-made routes**, not inventing new routes.

---

## 1. Product vision

Build `outdoor-agent`: a local, open-source-friendly command-line tool that enables an AI agent (OpenClaw) to discover **real, existing** community outdoor routes, evaluate them against user preferences, identify known or suspected road/trail closures, and prepare approved routes for transfer to a COROS watch.

A natural-language request such as:

> “Find five scenic 20–35 km gravel loops within 30 km of Utrecht for Sunday, prioritise routes others have ridden, flag any closures, and send my favourite to COROS.”

should yield ranked, evidence-backed options with source links, distance, climbing, activity, accessible GPX where supported, and uncertainty around closures. **Nothing is sent to a third-party account without explicit user approval.**

### Product principles

1. **Existing routes first.** Do not silently substitute AI-generated paths for community routes.
2. **CLI-first and JSON-first.** Deterministic machine interface for OpenClaw; no required web UI, server, MCP runtime, or additional model calls.
3. **Provider independence.** Adapters can be added, disabled, or replaced without changing CLI contracts.
4. **Uncertainty is explicit.** “No known closure” never means “guaranteed open.”
5. **Honest capabilities.** Unverified public search, GPX entitlement, or sync operations are marked unavailable or experimental.
6. **Privacy by default.** Local SQLite cache, no telemetry, secret redaction, minimal external calls.
7. **No surprise costs.** Google integrations opt-in, with budgets / quotas; rate limits across all providers.
8. **Human-approved side effects.** Discovery is read-only; account pinning, publishing, uploads, and delivery require approval.
9. **uv everywhere.** No pip, Poetry, pipenv, requirements.txt-driven installation, or ad hoc virtualenv workflow.

### Non-goals for v1

- Building another route-sharing social network.
- Scraping protected services to bypass authentication or paid access.
- Guaranteeing that every rural trail or pedestrian path is open.
- Automatically changing a community-created route without telling the user.
- Undocumented COROS Cloud APIs or direct proprietary watch control.
- Full web frontend, mobile app, or MCP server before the CLI stabilises.
- Permanent storage or bulk replication of restricted provider datasets.
- Assuming COROS synchronization to the account implies it reached the physical watch.

---

## 2. Users and supported activities

Primary user: a technically experienced homelab operator running OpenClaw on Ubuntu and using a COROS PACE 4.

Supported activities in the normalized model:
- hiking / walking
- trail running / running
- road cycling
- gravel cycling
- mountain biking
- other outdoor activities (e.g. kayaking, skiing), when a provider supports them

`other` activities are not to be sent through road-only closure logic as if they were bikes or cars. Each provider must declare its activity support and coverage.

Location input: human place name, coordinates, optional bounding box, radius, and origin/start proximity. Date/time input: ISO-8601 with timezone, default timezone **Europe/Amsterdam** only when none was supplied. Preserve date-only versus date-time precision in models.

---

## 3. Core user stories and acceptance criteria

### US1 — Discover community routes
Given a location, radius, activity, distance range, and optional route shape (loop / point-to-point), return up to N **real** community routes.

Acceptance:
- Each result has a stable internal ID; provider + provider route ID; original URL; human-readable name; source attribution; activity; start coordinates if available.
- Distances, elevation gain, GPX availability and ratings are nullable, never fabricated.
- Distinguish `community`, `official_trail`, and `generated` provenance; default discovery excludes `generated`.
- Support a deduplication strategy using geometry fingerprint / distance / proximity / name (conservative, with provenance retained).
- If a provider lacks verified public discovery access, emit a provider capability warning rather than fake results.

### US2 — Review a shortlist
Rank candidates by activity suitability, requested length, proximity, loop preference, route popularity/reviews **when verifiable**, freshness, known closures, and optional scenic POIs. Explain ranking factors. Missing popularity metrics should not be interpreted as low popularity.

### US3 — Inspect GPX
Read a local GPX file; compute geometry, track length and bounds; inspect track/route waypoints; validate coordinates; detect empty or malformed GPX; normalise multiple segments and preserve provenance. Never quietly “repair” geometry in ways that change the trip.

### US4 — Assess closures
For a GPX or stored route, planned departure date/time and activity, assess known conflicts and possible nearby conflicts using authoritative Netherlands feeds and optional OSM restrictions.

Acceptance:
- Return `known_conflict`, `possible_conflict`, `no_known_conflict`, `coverage_unknown`, or `check_failed` rather than one misleading boolean.
- Include event ID, issuer, source URL, access mode (road / bike / foot), time window, distance along route, affected geometry, severity and reason.
- Distinguish actual spatial intersection from near miss, parallel path, bridge/underpass and incomplete geocoding.
- Date-aware overlap. Unknown or stale event timing must be surfaced.
- A source outage must NOT turn a route “safe.”

### US5 — Export or hand off an approved route
Download/export GPX when licensed and supported, or provide a manual handoff path. Integrate with Ride with GPS → COROS pin/send only after verifying an authenticated, supported write action.

Acceptance:
- Side-effect actions require `--confirm` plus target/account summary (CLI); OpenClaw skill must ask first.
- Idempotency: repeated pin/send should not cause duplicate writes where a stable remote ID exists.
- Report `exported`, `submitted_to_provider`, `visible_in_coros_account` **only if verified**, and `watch_sync_pending` or `watch_sync_unverified`. Never claim physical watch delivery without evidence.
- If direct automated delivery is unavailable, emit user-readable manual instructions and a GPX path when legitimately available.

### US6 — Recheck saved routes
Allow rechecking selected routes for closure changes. Store previous finding hashes and compare to new findings; optionally expose `watch` via systemd timer/cron integration **later**. No daemon required in v1.

---

## 4. Provider strategy and gates

Every adapter must declare:

```json
{
  "provider": "ridewithgps",
  "capabilities": {
    "discover_public": "unverified",
    "get_metadata": "unverified",
    "download_gpx": "unverified",
    "pin_route": "unverified",
    "auth_required": true
  },
  "checked_at": null,
  "notes": "Capability probes required before production"
}
```

Capability state: `supported`, `unsupported`, `unverified`, `restricted`, `degraded`. Record probe timestamp and evidence links. A missing capability must never be inferred from a working adjacent operation.

### 4.1 Ride with GPS (high priority)

- Explore official [`ride-cli`](https://github.com/ridewithgps/ride-cli) and documented authenticated API.
- Official CLI supports direct `ride tool ...` and `ride api ...` operations, and is not synonymous with public route discovery.
- Use direct tool/API invocation from our Python adapter where suitable, safely escaping arguments; preferably invoke via `asyncio.create_subprocess_exec`, **never shell interpolation**.
- Do not make OpenClaw launch a separate Claude session (`ride ask`) for ordinary operations.
- Probe: authenticated access; public community search; metadata; GPX export of others’ public routes; save/pin; account visibility; rate limits/permission constraints.
- If the official API only accesses user-owned data, DO NOT advertise community search. Keep the adapter for owned/saved routes and COROS handoff.
- Canonical provider URL and original route authorship retained.
- Credential state surfaced as `auth_required` or `auth_expired` without printing tokens.

### 4.2 OpenStreetMap and route relations (high priority)

- Discover hiking/cycling route relations through **Overpass** and/or suitable OSM-derived services.
- These are often officially mapped trails, **not community reviews or user-created ride recommendations**. Label `official_trail` correctly.
- Use Nominatim-compatible geocoding or configured geocoder with local caching and user-agent identification; respect public service policies and request limits.
- Request a geographic bounding box and route relations by activity, reconstruct member ways in the correct order where possible; report gaps and disconnected segments.
- Avoid heavy, uncached continent-sized Overpass queries.
- Keep OSM attribution and ODbL obligations documented.

### 4.3 Wikiloc (optional research / later)

- Attractive broad activity library, but do not assume a supported public API.
- Phase 1: manual original URL / GPX import; research structured discovery access separately.
- Respect authentication, export entitlements, and site usage terms. No login bypass.
- Add browser-assisted research only when supported and permitted, never as a fragile core dependency.

### 4.4 Other community platforms (future)

AllTrails and Strava may serve as user-supplied route URL/import sources, but don't make a production adapter dependent on undocumented scraping or paid-access bypass. Providers may be disabled individually.

### 4.5 Netherlands NDW / Melvin (high priority for closures)

- Start with documented NDW open-data feeds for current closures and planned roadworks/events; verify **actual endpoints, schemas, availability and licenses at implementation time**.
- DATEX II/XML feeds may use location referencing rather than ready-to-use geometries: design a separate decoding stage.
- Normalize data into events with ID, geometry (or location reference + `unresolved` flag), start/end, recurrence, vehicle/mode restrictions, road name, issuer, metadata URL, timestamps and confidence.
- Never treat a road-level closure as a footpath closure without mode evidence.
- Some closures may be announced by municipalities, park managers or forestry organisations but absent from NDW. Unknown coverage must be explicit.
- Cache efficiently and expose `last_successful_refresh` and age.

Useful research starting points:
- <https://opendata.ndw.nu/>
- <https://docs.ndw.nu/handleidingen/Melvin/>

### 4.6 Google Maps Platform (optional)

The user has a Google Maps Platform API key already configured for OpenClaw. Support a distinct configuration for `GOOGLE_MAPS_API_KEY` or secure external injection; do not assume access to OpenClaw's internal secrets.

Possible functions:
- Place discovery / Google Places data to enrich near-route stops, viewpoints, attractions or trailheads.
- Place name / origin geocoding if enabled.
- Driving access and traffic-aware estimates when applicable.
- **Do not** treat Google Routes as a complete trail/road closure feed or as authoritative for hiking/cycling closure detection.
- Keep Google-specific metadata, source/provenance and refresh policy separated as needed by platform terms. Budget and usage controls are required.
- No unrequested paid Google calls during baseline unit tests.

---

## 5. Architecture

Use a **local Python package** exposing a `Typer` executable `outdoor-agent`, with Pydantic domain models and typed adapter protocols.

```text
outdoor-agent/
├── pyproject.toml
├── uv.lock
├── README.md
├── LICENSE
├── AGENTS.md
├── spec.md
├── .env.example
├── .gitignore
├── src/outdoor_agent/
│   ├── __init__.py
│   ├── cli.py
│   ├── config.py
│   ├── errors.py
│   ├── models/
│   │   ├── routes.py
│   │   ├── closures.py
│   │   └── responses.py
│   ├── providers/
│   │   ├── base.py
│   │   ├── registry.py
│   │   ├── ridewithgps.py
│   │   ├── osm.py
│   │   ├── ndw.py
│   │   ├── wikiloc_import.py
│   │   └── google_places.py
│   ├── geo/
│   │   ├── gpx.py
│   │   ├── geometry.py
│   │   └── closures.py
│   ├── services/
│   │   ├── discovery.py
│   │   ├── ranking.py
│   │   ├── validation.py
│   │   └── delivery.py
│   └── storage/
│       ├── db.py
│       └── migrations.py
├── skills/openclaw/outdoor-routes/SKILL.md
├── tests/
│   ├── fixtures/
│   ├── unit/
│   ├── contract/
│   └── integration/
└── .github/workflows/ci.yml
```

Minimum Python: 3.12. Use:
- `uv` for environment, dependencies, execution, publishing if later needed.
- `typer` CLI; `rich` optional for human output; `pydantic` v2 schemas.
- `httpx` async HTTP with bounded timeouts/retries and provider throttling.
- `gpxpy` for GPX; `shapely` and `pyproj` for geometry in appropriate projected CRS.
- `sqlite3` (stdlib) by default; migrations versioned. No Postgres for v1.
- `pytest`, `pytest-asyncio`, `ruff`, `mypy` for quality.
- Avoid heavyweight framework dependencies and unnecessary containers.

### 5.1 Provider boundaries

Define Python `Protocol`s / abstract base classes for:
- `RouteDiscoveryProvider`: `search(criteria) -> SearchResult`
- `RouteDetailProvider`: `get(route_id) -> Route`
- `RouteExportProvider`: `export_gpx(route_id) -> GPXArtifact`
- `ClosureProvider`: `list_events(bbox, window, mode) -> ClosureFeedResult`
- `PlaceEnrichmentProvider`: `near_route(geometry, kind) -> list[Place]`
- `DeliveryProvider`: `prepare(route)` and separately `commit(approved_request)`

Providers must expose actual support via capability registry, not raise undocumented exceptions for ordinary unsupported workflows.

Separate provider I/O from validation/scoring logic. Provide reproducible fixture-based adapters for tests; fixtures must be explicitly labeled fixtures and **never returned as live discoveries**.

### 5.2 Data model

`Route` minimum:
- `id` (stable internal UUID or provider-qualified key)
- `provider`, `provider_route_id`, `source_url`, `source_type`, `attribution`
- `name`, `description?`, `activity`, `geometry?` (WGS84 LineString/MultiLineString)
- `start_lat`, `start_lon`, `end_lat`, `end_lon`
- `distance_m?`, `elevation_gain_m?`, `is_loop?`
- `popularity?`, `rating?`, `reviews_count?`, each with provenance if present
- `created_at?`, `updated_at?`, `fetched_at`, `geometry_quality`
- `gpx_access` (`available`, `restricted`, `unverified`, `unavailable`)
- `raw_metadata` (bounded and privacy-scrubbed) and `warnings[]`

`ClosureEvent` minimum:
- `id`, `provider`, `source_url?`, `description`, `issuer?`
- `geometry?`, `location_reference?`, `geometry_quality`
- `starts_at?`, `ends_at?`, `recurrence?`, `timezone?`
- `affected_modes[]`, `restriction_type`, `direction?`, `status`
- `observed_at`, `published_at?`, `expires_at?`, `raw_metadata_reference?`

`ClosureFinding` minimum:
- `route_id`, `event_id`, `classification`, `reason`
- `distance_along_route_m?`, `min_distance_to_route_m?`, `overlap_length_m?`
- `temporal_overlap`, `mode_overlap`, `geometry_confidence`
- `source_url?`, `evidence[]`, `limitations[]`

A normalized route must not lose the provider's original source URL or creator attribution.

### 5.3 Geospatial correctness

- Store provider geometry in EPSG:4326; do distance/intersection operations in an appropriate local metric CRS (in the Netherlands, RD New/EPSG:28992 where appropriate) or geodesic method.
- Bounding-box expansion must account for latitude; do not equate WGS84 degrees to metres.
- GPX can have multiple tracks/segments, and disconnected paths: account for them.
- Densify/simplify geometry only with explicitly declared tolerance; keep original GPX untouched.
- A “within 20 m” proximity is a **potential** conflict, not proof of blockage.
- Check overlapping timing, supported transport mode, closure direction, grade separation when known, and ambiguity.
- An unmatched closure location or unresolvable DATEX II reference becomes `coverage_unknown` or `possible_conflict`, not a dropped event.
- Include geometry CRS, precision, and freshness metadata in reports.

---

## 6. CLI design

Single executable: `outdoor-agent`; `--json` supported for all information commands; `--quiet`, `--verbose`, `--config` and `--version`.

**Commands below are required targets, not existing implementations.**

```bash
# Diagnose providers and credentials without leaking secrets
uv run outdoor-agent doctor --json
uv run outdoor-agent providers --json

# Discover community routes; use only providers with verified search support
uv run outdoor-agent search \
  --near "Utrecht, Netherlands" \
  --radius-km 30 \
  --activity gravel \
  --distance-km 20:40 \
  --loop preferred \
  --limit 10 \
  --json

# Import an arbitrary GPX and inspect it
uv run outdoor-agent import-gpx ./samples/ride.gpx --json
uv run outdoor-agent inspect ./samples/ride.gpx --json

# Validate against closures for a given trip date
uv run outdoor-agent check-closures \
  --gpx ./samples/ride.gpx \
  --date 2026-10-11 \
  --activity gravel \
  --json

# Enrich with points of interest only when configured
uv run outdoor-agent places \
  --gpx ./samples/ride.gpx \
  --category viewpoint \
  --provider google \
  --json

# Export only when allowed by source capability and account access
uv run outdoor-agent export --route-id ridewithgps:12345 --format gpx --output ./routes/12345.gpx

# Dry-run and approved handoff (actual provider support required)
uv run outdoor-agent deliver \
  --route-id ridewithgps:12345 \
  --target coros \
  --dry-run \
  --json
uv run outdoor-agent deliver \
  --route-id ridewithgps:12345 \
  --target coros \
  --confirm \
  --json
```

For an unavailable function, respond with a typed error and remedial suggestions; do not return “success” with an empty list.

### 6.1 JSON contracts

Every command returning JSON must emit exactly one JSON object to stdout. Human diagnostic logs go to stderr. Never print progress bars to JSON stdout.

Successful example (illustrative schema, **not actual live routes**):

```json
{
  "schema_version": "1.0",
  "status": "ok",
  "request_id": "example-req-001",
  "generated_at": "2026-10-09T14:00:00Z",
  "data": {
    "routes": [],
    "total": 0
  },
  "provider_status": [
    {"provider": "osm", "status": "ok"},
    {"provider": "ridewithgps", "status": "unverified", "reason": "public search not verified"}
  ],
  "warnings": []
}
```

Error example:

```json
{
  "schema_version": "1.0",
  "status": "error",
  "error": {
    "code": "PROVIDER_CAPABILITY_UNVERIFIED",
    "message": "Public community route search has not been verified for this provider.",
    "provider": "ridewithgps",
    "retryable": false
  }
}
```

Partial success must be `status: "partial"` with failures and coverage disclosures. Suggested exit codes:
- `0`: complete success
- `2`: invalid arguments / bad input
- `3`: credentials / permissions
- `4`: provider unavailable / rate limited
- `5`: unsupported capability / entitlement
- `6`: partial or insufficient coverage in strict mode
- `7`: internal error

Store and publish versioned JSON Schemas; contract tests should pin examples.

### 6.2 Preferences & ranking

Weighted scoring with configurable weights; use explanatory sub-scores rather than arbitrary black box values:
- distance fit
- travel/start proximity
- preferred activity/terrain
- loop preference
- popularity or review evidence when present
- closure risk and verification confidence
- route freshness
- scenic POI relevance (optional)

Scoring must not privilege providers simply because their API exposes more metrics. Explicitly explain missing measurements and avoid “zero stars” for missing ratings. Show 3–5 best options with a transparent reason each.

---

## 7. Closure checking — detailed requirements

### 7.1 Feed pipeline

1. Fetch NDW/Melvin or other configured feeds with timeouts, retries, bounded memory, decompression limits, source timestamps.
2. Parse/version DATEX II XML and validate input structure.
3. Convert location reference to geometry using the correct documented method; record unresolved cases.
4. Normalise temporal schedule and mode restrictions.
5. Spatially prefilter closure events against candidate route bounding box.
6. Compute exact or buffered intersections in metres; distinguish `intersects` from `nearby`.
7. Compare activity mode, date/time overlap, direction, grade separation if available.
8. Return evidence, confidence and source freshness.
9. Cache provider feed provenance, findings and last successful update.

### 7.2 Assessment policy

Priority (highest to lowest):
- `known_conflict`: sufficiently precise geometry, active temporal overlap, activity-specific restriction.
- `possible_conflict`: insufficient mode/geometry, nearby reporting, partial reference decode, or ambiguous opening hours.
- `no_known_conflict`: sources successfully checked, no matching evidence **with source coverage disclosed**.
- `coverage_unknown`: expected sources absent, stale or unresolved such that conclusion cannot be drawn.
- `check_failed`: technical failure; do not interpret as pass.

For multiple events, return per-event findings plus conservative aggregate assessment. A strong known conflict wins, but report additional unknown coverage. Add configurable buffer radius with defaults chosen through fixtures, not magic values.

### 7.3 Test scenarios

- Road closure applies to cars but an adjacent separated cycle path remains open.
- Shared cycleway genuinely closed for bicycles, route intersects during closure window.
- Path closure ends before user's planned ride begins.
- User's planned date is beyond feed's reliable horizon.
- Parallel road within 15 metres produces only possible conflict.
- Bridge crossing above closed road does not automatically imply closure.
- Missing geometry / DATEX referencing failure.
- Source reports no data because of outage; result must not be `no_known_conflict`.
- Overnight closures crossing midnight or daylight-saving change in Europe/Amsterdam.
- Trail-only closure present in OSM but absent in NDW.
- Remote route outside the Netherlands reports coverage limitations.

---

## 8. COROS PACE 4 delivery

Start from supported, documented flows:

**Flow A: Ride with GPS → COROS account**
- User connects accounts through official channels.
- Official Ride with GPS help describes sending or pinning routes to COROS.
- Verify whether the CLI/API offers an authorised pin/send write operation.
- After a successful remote write, report submission separately from downstream sync confirmation.
- The user may still need to select and sync the route to the physical watch through the COROS app.

**Flow B: GPX → COROS app**
- Export/download a permitted GPX file and provide file path plus concise import guidance.
- Do not conflate GPX navigation with offline map-tile installation (which has separate connectivity requirements).

**Never** store COROS passwords, emulate private endpoints or claim direct watch installation unless there is a documented supported API and verification.

Idempotency keys based on route+target+account. Explicit dry-run default for side effects. Always give a human recovery path.

References:
- <https://support.ridewithgps.com/hc/en-us/articles/27231998487963-COROS>
- <https://support.coros.com/hc/en-us/articles/28320578199060-Connecting-Ride-with-GPS-to-COROS-Account>

---

## 9. OpenClaw integration

The OpenClaw integration is a **thin skill** over the deterministic CLI. It must not contain business logic duplicated from Python.

Add `skills/openclaw/outdoor-routes/SKILL.md` with documented YAML frontmatter:
- `name: outdoor-routes`
- `description`: find existing routes, check known closures and prepare approved COROS handoffs
- Requirements on executable path where supported
- Examples invoking `uv run outdoor-agent ... --json` from a known project root, or installed `outdoor-agent`
- How to interpret partial coverage and warnings
- Confirmation requirements for write operations
- No invented routes, ratings, access claims, or completed synchronizations
- Rate-limit / quota / privacy etiquette
- Explicit reminder to quote source URLs and distinguish community versus official trails

Suggested agent flow:
1. Parse user preference, location, activity, distance, intended date/time.
2. Invoke `providers` / `doctor` when uncertain.
3. Search community providers first; supplement with mapped official trails.
4. Rank and provide linked shortlist.
5. Check closures for final candidates (or earlier to prune results) and disclose feed freshness.
6. Ask which route to send.
7. Execute dry-run, present target account/side effects, then commit approved delivery.
8. Report verified provider status and any manual COROS action required.

Skill must be easy to copy or symlink into OpenClaw workspace skill directory; document required current OpenClaw skill conventions:
- <https://docs.openclaw.ai/tools/creating-skills>
- <https://docs.openclaw.ai/skills>

No second LLM needed inside the CLI. Add MCP only as optional follow-up.

---

## 10. Configuration, security & operations

Provide `.env.example` with placeholders only; environment is not the sole config source. Prefer XDG config/data/cache paths on Ubuntu, with overridable dirs:

```dotenv
OUTDOOR_AGENT_DATA_DIR=
OUTDOOR_AGENT_CACHE_DIR=
OUTDOOR_AGENT_DEFAULT_TIMEZONE=Europe/Amsterdam
OUTDOOR_AGENT_DEFAULT_REGION=NL
RIDE_CONFIG_DIR=
GOOGLE_MAPS_API_KEY=
OUTDOOR_AGENT_GOOGLE_ENABLED=false
OUTDOOR_AGENT_MAX_GOOGLE_REQUESTS_PER_RUN=20
OUTDOOR_AGENT_HTTP_TIMEOUT_SECONDS=20
OUTDOOR_AGENT_OFFLINE=false
```

Security requirements:
- No credentials or personally identifying track content in logs by default.
- No shell injection; strict provider URL allowlisting / URL safety for any downloads and CLI subprocess.
- Bounded GPX/XML payload sizes, decompression limits and geometry complexity.
- HTTPS, reasonable retries/backoff, per-source throttling, cache TTLs.
- Configurable quotas and opt-in paid API calls.
- Dry-run before remote writes, explicit confirmation, idempotent behaviour.
- Git ignore secrets, local databases, watch files, generated GPX, and `.venv`.
- Include operational guidance for SSH service users, least privilege, filesystem permissions and backup of SQLite config/state.
- No exposed HTTP listener in v1.

Document which external calls send route locations or user preferences and which are local-only.

---

## 11. uv-only development policy

All Python dependency, environment and script execution uses `uv`.

Project start:

```bash
uv init --package --name outdoor-agent --python 3.12 .
uv add typer pydantic httpx gpxpy shapely pyproj
uv add --dev pytest pytest-asyncio ruff mypy
uv sync --dev
uv run outdoor-agent --help
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv lock --check
```

Notes:
- Commands assume initialization within an empty repository; Codex should detect existing files and merge appropriately rather than clobber.
- Commit `uv.lock` and `pyproject.toml`.
- Keep runtime dependencies minimal. Optional groups/extras: `[google]`, `[ridewithgps]` only if justified.
- No `pip install`, `python -m venv`, Poetry, Pipenv, conda or requirements.txt as source of truth.
- Use `uv run` for tests, lint, application and utility scripts; `uv tool` only for unrelated standalone tools where needed.
- In GitHub Actions: install `uv`, `uv sync --frozen --dev`, `uv run --frozen pytest`, `uv run --frozen ruff check .`, `uv run --frozen mypy src`; CI must work without API credentials.

### Definition of done for each PR
- Typed, documented public interfaces.
- Tests for success, failure and partial/unknown states.
- No live provider network dependencies in unit tests.
- CLI examples run under `uv`.
- JSON output schema kept backwards compatible or version bumped.
- README, `spec.md`, and OpenClaw skill updated when relevant.
- No claims of provider support unless capability probe evidence is documented.
- No secret leakage.

---

## 12. Test strategy

**Unit**: GPX parsing, CRS transforms, distance calculations, date windows/DST, scoring, typed errors, JSON schemas, URL safety, unknown-coverage policy, config.

**Contract**: Fixture responses for each provider, including API change, restricted data, 401/403/429/500, malformed XML, missing geometry, paginated results, duplicates.

**Integration**: opt-in network tests behind explicit environment marker; never run credentialed or paid Google operations in generic CI. Probe documented public Ride with GPS API capabilities with user consent and log redacted evidence.

**End-to-end offline**: feed fixture + GPX route fixture + OpenClaw-style CLI calls with JSON verification, closure issue warning, dry-run transfer.

At least one positive closure test, one false-positive avoidance test, one unavailable feed test and one cross-midnight/DST test should pass before calling closure checking ready.

CI on Ubuntu using `uv`; coverage measured for core geo and data model modules; no requirement to hit an arbitrary 100% number.

---

## 13. Incremental delivery plan

### Phase 0 — Provider feasibility spike (do first, short and evidence-based)
- Inspect exact current documentation for Ride with GPS API and `ride-cli`.
- Establish whether public community search and lawful GPX retrieval are actually accessible to our agent.
- Verify NDW endpoints, schema version, geometry decoding path and update cycle.
- Verify OpenClaw skill format and COROS handoff.
- Record evidence and capability matrix in `docs/provider-feasibility.md`.
- **Decision gate:** if Ride with GPS public discovery is not available, prioritize OSM / other supported community providers; do not block entire build.

### Phase 1 — Functional CLI foundation
- `uv` Python package, source layout, models and Pydantic schemas.
- Typer CLI, structured logging/exit codes, XDG SQLite cache.
- Local `import-gpx`, `inspect`, `providers`, `doctor`, schema export.
- Complete offline tests, CI, README, `.env.example`, `AGENTS.md`.
- OpenClaw skill skeleton with commands that actually work.

### Phase 2 — Live discovery
- OSM route relation discovery; reliable route geometry reconstruction.
- Ride with GPS adapter *only for verified operations*; auth setup and tested errors.
- Source-aware ranking/deduplication; community vs official-trail labels.
- User-provided GPX/URL import where supported.
- Integration docs with example real queries.

### Phase 3 — Closure analysis
- NDW/Melvin fetch+parse+location resolution; freshness metadata.
- Mode/date-specific geospatial analysis, uncertainty handling.
- CLI `check-closures`; real and fixture-based checks; cached feed updates.

### Phase 4 — Enrichment and delivery
- Opt-in Google Places enrichment, request budget.
- Verified GPX downloads / exports.
- COROS handoff by official Ride with GPS integration (if supported); otherwise manual GPX path.
- Approval gates, audit trail and idempotency.

### Phase 5 — Reliability
- Saved route rechecks, diff alerts via CLI, optional systemd timer.
- Optional MCP server and extended provider ecosystem after CLI API stabilizes.
- Sample OpenClaw prompt recipes and deployment guide.

---

## 14. Risks, mitigations, open questions

| Risk | Mitigation |
|---|---|
| Ride with GPS API doesn't support public route searching | Probe early; OSM fallback; mark unsupported transparently |
| Community route GPX downloads require entitlement | Honor entitlement, support URL/metadata discovery and manual export |
| Road closure feed references are not geometry | Separate location decoding; never equate nearby event point with actual closure |
| Trails not included in NDW | Surface coverage gaps, add OSM/local authority sources later |
| COROS remote pin available only via UI | Manual GPX or user-initiated official sync; no private API |
| Google API unexpected costs | Disabled by default, per-run budget, caching and observability |
| Overpass rate limits / huge routes | Spatial bound, cache, throttling and provider backoff |
| Provider terms change | Per-adapter capability check, provenance, tests and feature flags |
| OpenClaw has broad system permissions | Restrict skill to known CLI arguments and safe paths |
| Public source has stale route geometry | Display collection date, provenance and unknown access conditions |

Open design questions to resolve by probing rather than guessing:
1. Which public community catalogs allow structured route search and export?
2. What is the NDW feed's exact supported DATEX II geometry and location-reference format?
3. Can an authenticated Ride with GPS route be pinned by a documented API endpoint?
4. Which non-road local trail-management data feeds are useful for the user's actual regions?
5. Can we verify downstream COROS account visibility via a documented endpoint, or only provider submission?
6. What popularity metrics and reviews are legally and technically accessible?

---

## 15. Acceptance demo (end-to-end)

A meaningful v1 demo:

1. On Ubuntu, `uv sync --frozen --dev` succeeds without system pip.
2. `uv run outdoor-agent inspect fixtures/example.gpx --json` emits valid, schema-compliant geometry metadata.
3. A configured route search returns **real, linked, attributed** routes, or clearly explains why providers cannot search.
4. `check-closures` on fixture GPX produces verifiable `known_conflict` / `possible_conflict` with route chainage and temporal reason.
5. Outage simulation results in `coverage_unknown` or `check_failed` rather than success.
6. Dry-run COROS delivery shows side effects without taking any action.
7. An approved route can be exported as a valid GPX, or transferred using a verified supported provider integration.
8. OpenClaw skill invokes the CLI, lists real candidates with original links, explains closure uncertainty and requests approval.
9. Running `uv run pytest`, `uv run ruff check .`, and `uv run mypy src` succeeds in CI.
10. README demonstrates a clean Ubuntu install and no credentials checked in.

**Success definition:** A real community route goes from discovery to closure assessment to a COROS-compatible handoff with provenance, explicit uncertainty, and no hidden network or account side effects.

---

## 16. Instructions for Codex

> You are implementing `outdoor-agent` in `agbocsardi/outdoor-agent`. Follow this specification, favour working end-to-end increments over giant speculative architecture commits.
>
> **Start by inspecting the repository** and existing code. Preserve any user-created files. Use **uv exclusively** for Python environments, package management and execution.
>
> First perform **Phase 0 feasibility**, documenting facts and uncertainties from official sources. In the same initial implementation work, complete **Phase 1**: a functioning installable CLI, GPX inspect/import, consistent JSON output, test suite, CI, README, AGENTS.md and OpenClaw skill skeleton. Do not stop after writing architecture documents.
>
> Implement providers behind explicit capability checks. Never fabricate community routes, road closures, provider API support, download privileges or COROS delivery. Use fixture data only in tests or clearly identified demo mode. Ensure live user-facing search doesn't silently return fixture routes.
>
> Always run `uv sync --dev`, `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`, and `uv run mypy src` (and address failures). If repository restrictions prevent a command, explain the blocker.
>
> Make small coherent commits / PRs; document what is genuinely implemented, what remains unverified, exact setup commands, and how to test each milestone. Ask for human approval before any third-party account writes or secrets.


## Implementation status — 2026-10-09

Version 0.2.0 implements the CLI through Phase 5: all command targets, saved route
metadata, live OSM/Nominatim discovery, transparent ranking/deduplication, authenticated
documented RWGPS reads/export, NDW direct-GML closure assessment, rechecks and change
comparisons, opt-in budgeted Google Places, lossless local GPX export, and confirmed,
audited, idempotent manual COROS GPX handoff. Versioned schemas and the OpenClaw skill
cover these workflows. See `docs/cli-demo.md` for live and offline verification.

Provider gates remain explicit: no verified public community-route search is configured;
OSM results are mapped `official_trail` records. Alert-C references are retained unresolved,
not falsely decoded, and NDW trail/future coverage is uncertain. Google and authenticated
RWGPS contracts are tested offline; no paid Google request or credentialed account probe
was performed. COROS account submission and physical watch arrival are unverified; the
implemented handoff is manual GPX. OpenClaw host loading, a systemd timer and MCP are
operator/optional follow-ups. These limits do not silently produce fixture recommendations
or claims of complete coverage or delivery.
