# Specification acceptance and feasibility gates

Audit date: 2026-10-09. This matrix separates requirements implementable and testable in the repository from capabilities that depend on an external provider, account, agreement, or target host. It follows `spec.md` §§3–5, 8–9, 13, and 15. It records evidence and remaining gates; it is not a release sign-off.

## Acceptance matrix

| Area | Gate | Acceptance condition | Current evidence / status |
|---|---|---|---|
| Capability registry and provider boundaries | **Required code gate** | Declare each operation independently with state, check date, evidence, auth requirement, and limitations. A working read must not imply search, export, or write support. | Registry and adapters are present in the current tree. Preserve `unverified`/`restricted` for capabilities without the relevant probe. |
| Public community route discovery | **External gate for public-catalog discovery** | At least one lawful, structured public source returns real community routes with source URL, authorship/provenance, and permission-limited GPX status. Otherwise the CLI must return a typed unavailable/partial result and must not claim full success. | **Public catalog not achieved.** Local saved-catalog search is implemented for user-imported community routes with supplied provenance; it does not establish public access or independently verify authorship. No configured source has verified public community search. RWGPS public Explore is not a documented API; its `GET /api/v1/routes.json` index is scoped to the authenticated user's own saved routes (read-only probe 2026-10-10, empty account) and is labeled account-scoped, not a public catalog. OSM Overpass returns mapped route relations labeled `official_trail`, not community rides/recommendations. RouteYou offers documented route search/info services but requires a key and agreement. Details are in [provider feasibility](provider-feasibility.md) and [discovery](discovery.md). |
| OSM read-only route data | External source available; **required code gate for the official-trail fallback** | Use bounded Overpass relation reads, honor service limits, retain ODbL attribution, reconstruct member ways without bridging gaps, and label mapped routes `official_trail`. Do not use the OSM core Editing API for read-only discovery. | A public Overpass probe returned route relations on 2026-10-09. OSMF policy says its core API is for editing, not read-only projects, and points read-only uses to Overpass or extracts. Public GPS traces have separate privacy classes and are not established here as a supported recommendations/search catalog; they are not used as a workaround. |
| Search and ranking | **Required code gate** | Support bounded geographic criteria, activity, distance, start proximity, loop and point-to-point preferences, deduplication/provenance, and transparent ranking. Missing popularity/freshness must remain missing rather than count as zero. Apply date-specific closure ranking only when evidence is available and explain absent factors. | Search/ranking code and OSM relation mapping are in the current tree. Offline tests exercise geographic/shape criteria, source filtering, provenance, missing metrics and dated closure ranking. A real OSM query demonstrates endpoint access, not catalog completeness or community quality. |
| GPX import and local storage | **Required code gate** | Preserve original bytes and first provenance; validate size, XML safety, geometry, and coordinates; retain tracks, routes, waypoints, elevation gaps, and disconnected segments; keep storage private and imports idempotent. | Implemented with offline unit coverage in the current tree. No external provider is needed for the local-file path. |
| NDW closure analysis | **Required code gate; public source is available** | Parse bounded current/planned feeds; preserve mode, time, recurrence/revision, geometry, freshness and issuer evidence; distinguish intersects from near misses; return unknown/failed when evidence or coverage is insufficient. Test stale source, outage, bridge/grade separation, recurrence/DST, and unresolved references. | Public NDW feed probes on 2026-10-09 returned DATEX II v3 data. Current documentation reports direct GML support and unresolved Alert-C references. Feed cadence, forecast horizon, trail/park/forestry completeness, and an update SLA remain unverified; `no_known_conflict` cannot imply every path is open. |
| OSM restriction enrichment | **Required code gate for the current implementation plan; supplementary evidence only** | Query only bounded route-adjacent features, preserve OSM attribution/freshness, and classify restrictions conservatively. It cannot replace NDW or prove a route is open. | Adapter and regression tests are present in the current tree; the same read-only Overpass policy applies. Coverage remains limited to mapped/tagged restrictions. |
| Authenticated RWGPS | **Optional provider gate unless a selected route depends on it** | Before claiming owned/saved metadata or GPX export works, test with an explicitly configured account and route for which the user has permission. Record 401/403/expired states without printing tokens. No public search or remote write may be inferred. | **Partly probed.** A read-only `GET /api/v1/routes.json` (`listRoutes`) probe of the current account on 2026-10-10 returned an empty page (record_count 0, next_page_url null); this establishes the account-scoped listing endpoint and its empty-account behavior, not that the account can read/export any given route. The unauthenticated API probes returned 401. No public search or remote write is inferred. |
| Export and COROS handoff | **Required manual fallback; automated provider delivery optional** | Default to a dry-run plan. Require explicit confirmation for local output/handoff actions, identify the route and path, preserve original bytes when they exist, and report `watch_sync_unverified` until the user verifies the watch. Provide a permitted GPX import path and concise COROS app instructions. | Official COROS support documents opening a standard GPX in the COROS app, saving it, and then syncing it to a device. This makes user-mediated GPX handoff an allowed fallback; it does not prove RWGPS export entitlement or device delivery by the CLI. COROS Partner API route push is a separate restricted partner application requiring credentials and terms; no access or push was tested. |
| OpenClaw skill file | **Required code/documentation gate** | Provide a thin skill with valid `SKILL.md` YAML frontmatter and CLI examples that match current commands, confirmation rules, and uncertainty behavior. | Project skill file exists. Current OpenClaw docs (checked 2026-10-09) require a skill directory with `SKILL.md`, `name`, and `description`. |
| OpenClaw target-host loading | **Required external deployment gate for the OpenClaw acceptance demo** | Install/copy the skill under the target workspace `skills/` directory (or explicitly allow a symlink target), then verify it appears in that host's skill inventory and can invoke the installed CLI. | **Unverified.** No target OpenClaw runtime was available in this audit. Current docs enforce workspace containment: a symlink resolving outside the configured roots is skipped unless `skills.load.allowSymlinkTargets` trusts it. The repository file alone does not demonstrate host loading. |
| Google Places | **Optional, paid external gate** | Remain disabled by default; use a separately configured key, per-run hard quota, terms-aware handling, and no live paid baseline tests. It is enrichment only and never a closure source. | Contract/mock tests do not establish a live account entitlement. No paid request was made. |
| Scheduled rechecks, MCP, broader catalogs/closure feeds | **Optional / later phase** | Add only after CLI and provider contracts stabilize; expose coverage and user-controlled side effects. | Spec assigns these to later phases. They do not block a truthful local CLI/manual handoff, except that another community source is still needed for the product success definition. |

## Release interpretation

The internal acceptance gates are the CLI/data contracts, explicit capability states, bounded discovery, source-aware ranking, privacy, closure uncertainty, and a dry-run-first manual GPX handoff. The external gates are route-provider permissions, any partner agreements/credentials, and loading the skill on the actual OpenClaw host. Google Places, a COROS partner push, a RouteYou agreement, saved-route polling, and MCP are optional integrations.

The software supports honest fallback behavior by explaining unavailable public
community discovery, labeling OSM trails `official_trail`, and supporting entitled
GPX plus manual COROS import. This does **not** establish a ride-ready gravel
recommendation or full public community-catalog discovery. The 0.3.1 demonstration
in [CLI evidence](cli-demo.md#031-gravel-workflow-review) took a real club-published
26.2 km loop near Den Bosch through saved discovery, live closure assessment and
lossless local COROS-compatible preparation. Its source flags cycling restrictions,
and the live check returned possible conflicts with unknown coverage. It establishes
that software path with honest warnings, not route access permission or watch arrival.
Public-discovery success still requires permitted catalog access. OSM official
trails and an unprobed RWGPS account do not establish those external capabilities.

## Official external evidence

- **OSM API policy (checked 2026-10-09):** <https://operations.osmfoundation.org/policies/api/> distinguishes the core data-editing API from read-only services and identifies Overpass as read-only under its own limits. Public trace privacy categories are documented at <https://wiki.openstreetmap.org/wiki/Visibility_of_GPS_traces>.
- **RouteYou (checked 2026-10-09):** <https://help.routeyou.com/en/topic/view/114/web-services> lists route search/information web services. <https://help.routeyou.com/en/topic/view/373/your-app-to-routeyou-link> describes receiving a service key and signing an agreement; <https://help.routeyou.com/en/topic/view/330/route-web-service-2-0> directs users without an agreement to RouteYou sales. This is a future restricted option, not current access evidence.
- **Ride with GPS:** <https://ridewithgps.com/api/v1/openapi.yaml> documents API routes and authentication; the route index is for the authenticated user's routes. Read-only probes and the `ride-cli` review are recorded in [provider feasibility](provider-feasibility.md). No authenticated request was made on 2026-10-09.
- **COROS manual GPX:** <https://support.coros.com/hc/en-us/articles/360040243352-Troubleshooting-GPX-Route-Imports-for-Breadcrumb-Navigation> documents import in the COROS app and user-initiated device sync. Partner route push requires separate onboarding: <https://support.coros.com/hc/en-us/articles/53181766856724-Partner-API-Access>.
- **OpenClaw (checked 2026-10-09):** <https://docs.openclaw.ai/tools/creating-skills> documents skill format; <https://docs.openclaw.ai/tools/skills> documents workspace loading and local skill installation; <https://docs.openclaw.ai/tools/skills-config> documents trusted symlink targets for workspace roots.

## Review corrections — 2026-10-10

Authenticated Ride with GPS discovery now lists only the authenticated account's own
saved routes (`GET /api/v1/routes.json`, paginated, bounded to 20 pages and 500
records). Public discovery remains unavailable; explicit public route URLs and local
GPX imports are the supported seeds. The documented endpoint is account-scoped, so a
selected saved-only provider emits an explicit scope warning and the
`--source-type community` gate still requires `--provider local`. A read-only probe of
the current account on 2026-10-10 returned an empty page, recorded as dated evidence.

## Repository verification — 0.3.0

The final offline run passed 270 tests with one explicitly opted-in live test skipped, 90% full-package coverage, and clean Ruff lint/format, strict mypy and lock checks. `uv sync --dev` and `uv build` succeeded. The installed wheel ran `doctor --json` from outside the checkout. The explicit synthetic community catalog contract covers import → bbox/shape/date-aware discovery → uncertainty disclosure → dry-run → confirmed lossless manual GPX handoff; it is labeled synthetic and does not prove public community access.


## Review corrections — 0.3.1

Unlocatable national records now use bounded coverage summaries instead of individual
route findings. Reports retain at most 100 prioritized route findings; counts and
stable digests preserve overflow evidence and recheck change detection. Invalid
OSM member coordinates reject the whole member; length/loop claims require complete
geometry. Earlier cached OSM geometry and exports require explicit safe refresh.
Search exposes coverage/filter counters, and imported source-access caveats survive
search and dry-run handoff. The offline suite passed 283 tests (one opt-in live test
skipped), with 90% coverage. The real-source demonstration above is additional
evidence for the saved-route fallback, with unresolved access and coverage limits.
