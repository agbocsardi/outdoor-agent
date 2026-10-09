---
name: outdoor-routes
description: Inspect and import existing outdoor GPX routes, report provider capabilities, and prepare for future closure checks and approved COROS handoffs.
metadata: {"openclaw":{"requires":{"bins":["uv"]}}}
---

# outdoor-routes

This is the Phase 1 skeleton for the deterministic outdoor-agent CLI. Configure the
actual project root; the examples use `/srv/outdoor-agent`. Keep this root fixed and
quote file paths/arguments. Use `uv run --project /srv/outdoor-agent outdoor-agent`
from any working directory, or an explicitly installed `outdoor-agent` executable.
No second model session or `ride ask` is needed.

## Available workflow

1. Diagnose locally when unsure:
   `uv run --project /srv/outdoor-agent outdoor-agent doctor --json`.
2. Read capability evidence:
   `uv run --project /srv/outdoor-agent outdoor-agent providers --json`.
   Check both `implemented` and the requested capability's state; a documented
   provider feature alone does not make an adapter available. Auth is unverified.
3. Inspect a user-supplied, legally obtained file:
   `uv run --project /srv/outdoor-agent outdoor-agent inspect "/safe/path/ride.gpx" --activity gravel --json`.
4. Import to local SQLite/artifact storage when the user requests saving:
   `uv run --project /srv/outdoor-agent outdoor-agent import-gpx "/safe/path/ride.gpx" --activity gravel --json`.
   Include the original `--source-url`, verified `--source-type` and `--attribution`
   when known. GPX alone does not prove community authorship; retain unknown provenance.
5. Read schemas when needed:
   `uv run --project /srv/outdoor-agent outdoor-agent schema --name inspection --json`.

Read exactly one JSON object from stdout; diagnostics belong to stderr. Show original
source URLs and distinguish `community`, `official_trail`, `generated` and `unknown`.
Null distance/elevation/popularity/rating is unknown, never zero or a fabricated value.
Multiple track segments remain separate and may be disconnected. Fixtures are synthetic
and never count as live discoveries or recommendations.

## Future phases and safety

Search, closure checking, places, provider export and deliver currently return an
explicit `CAPABILITY_NOT_IMPLEMENTED` error (exit 5). Do not present an empty success
list, invent routes/ratings, infer supported public search from personal-route access,
or claim closures have been checked. Explain the limitation and offer local GPX import.

When future closure adapters become available, interpret `partial`, provider failures,
staleness and coverage warnings conservatively. `no_known_conflict` does not guarantee
an open route. `coverage_unknown` and `check_failed` are not a pass. Other activities
must not inherit road/bike restrictions without mode evidence.

Any future third-party pin/send/upload action requires the user to approve the specific
route, target account and side effects first. Run a supported dry-run, present its plan,
then use `--confirm` only for the approved request. Never invoke unverified endpoints
or ask for secrets in chat. Provider submission, COROS account visibility and physical
watch synchronization are separate claims; require evidence for each. Today the
operator must use official export/app flows manually if they want a COROS handoff.

No paid Google requests unless explicitly enabled with a separate injected key and
budget. Respect provider rate limits, cache TTLs, entitlements and privacy. Do not copy
personal tracks or credentials into logs, bulk scrape protected catalogs, or expose a
server. Runtime Phase 1 commands are entirely local.

Official skill conventions: https://docs.openclaw.ai/tools/creating-skills and
https://docs.openclaw.ai/skills. This file's format was researched; target-host runtime
loading is still unverified.
