# Completed CLI demo — 2026-10-09

The 0.2.0 implementation was exercised through the public provider endpoints,
with isolated state and cache directories. No credentials, paid Google calls,
third-party account writes or watch synchronization were used.

```bash
export OUTDOOR_AGENT_DATA_DIR=/tmp/outdoor-agent-demo/state
export OUTDOOR_AGENT_CACHE_DIR=/tmp/outdoor-agent-demo/cache

uv run outdoor-agent search --near "Utrecht, Netherlands" --radius-km 5 \
  --activity hiking --source-type official_trail --limit 3 --json
```

This run returned three real OSM route relations, with links and ODbL attribution:

| Relation | Mapped name | Source |
|---|---|---|
| 11569948 | 87-84 | https://www.openstreetmap.org/relation/11569948 |
| 11556654 | 86-95 | https://www.openstreetmap.org/relation/11556654 |
| 11556653 | 43-97 | https://www.openstreetmap.org/relation/11556653 |

The result was `partial`: the bounded Overpass query reached its 200-relation
limit and some member geometry was incomplete/disconnected. The output retained
these warnings and ranked only observable measurements. These are mapped trails,
not community recommendations, and live results will change.

```bash
uv run outdoor-agent get --route-id osm:relation:11569948 --json
uv run outdoor-agent check-closures --route-id osm:relation:11569948 \
  --date 2026-10-11 --activity hiking --json
uv run outdoor-agent export --route-id osm:relation:11569948 \
  --output /tmp/outdoor-agent-demo/11569948.gpx --confirm --json
uv run outdoor-agent inspect /tmp/outdoor-agent-demo/11569948.gpx --json
uv run outdoor-agent deliver --route-id osm:relation:11569948 \
  --target coros --dry-run --json
```

Both live NDW endpoints were fetched and parsed without source errors, with fresh
data and EPSG:28992 metric comparisons. The assessment returned `coverage_unknown`,
not a clean bill of health: 4,547 active, potentially relevant records had
unresolved location/mode evidence, and footpath/trail coverage was not established.
The exported GPX was 4,855 bytes and parsed successfully. `original_bytes` was
false because it encoded OSM geometry; it retained attribution, the source link
and disconnected segments. Delivery remained a read-only manual handoff plan.

The offline end-to-end contract suite separately verifies an explicit synthetic
cycleway closure as `known_conflict`, a nearby/crossing measure as possible,
car-only exclusions, outages, recurrence uncertainty, historical/future dates,
DST, cancellation revisions, changed-event comparisons, original GPX byte
preservation, protected output files, dry-run behavior and confirmed local
handoff idempotency. Fixtures are never substituted for live recommendations.

The explicitly opted-in public integration test also passed:

```bash
OUTDOOR_AGENT_LIVE_TESTS=1 uv run pytest -m live
```

RWGPS authenticated operations and Google Places are validated with offline
contracts. Actual account permissions, a paid Google request, OpenClaw host
loading and physical COROS watch arrival remain outside this verification.

A subsequent explicit `get --refresh` encountered transient Overpass server failures.
After bounded retries it returned the redacted, retryable `PROVIDER_UNAVAILABLE`
error with exit 4; the saved route remained available. This illustrates the
provider outage path and why a successful public probe is not an uptime guarantee.

## 0.3.0 acceptance pass

The full offline suite passed 270 tests (one opt-in live test skipped), with 90%
package coverage. uv sync/build/lock, Ruff lint/format and strict mypy passed. The
0.3.0 wheel ran `doctor --json` from outside the repository. The new saved-catalog
contract runs an explicitly synthetic community GPX through geographic/shape/date
filters, closure uncertainty, a dry-run and a confirmed lossless COROS handoff.
It does not establish public community access or real watch arrival.

A new public dated search was executed without account credentials or paid calls:

```bash
uv run outdoor-agent search --bbox 52.08,5.11,52.10,5.13 \
  --activity hiking --date 2026-10-11 --source-type official_trail \
  --limit 3 --json
```

It returned schema-valid `partial` output with real attributed OSM relations
11569948, 11556654 and 18961261. Every returned candidate had a date-specific
closure summary with `coverage_unknown`; unknown coverage was excluded from the
closure score. NDW snapshots were fetched/parsed once for the eligible candidate
batch before the shortlist limit. A separate bounded optional OSM restriction
probe returned 599 normalized events with no source errors; the events retain
unknown timing and incomplete trail coverage. See [spec acceptance](spec-acceptance.md)
for external gates still requiring operator/provider access.
