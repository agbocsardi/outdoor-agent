# SSH deployment and OpenClaw recipes

Use a dedicated, unprivileged Linux user. Clone the repository into that user's
project directory, install uv, and run `uv python install 3.12` followed by
`uv sync --frozen --dev`. Use `uv run --project /srv/outdoor-agent outdoor-agent`
as the fixed executable prefix if that is the chosen installation directory.

Keep `~/.config/outdoor-agent/config.toml` and injected credential files at mode
600, and state/cache directories at mode 700. The CLI creates private files;
operators should also restrict existing directories. Configure trusted provider
endpoints. Start with Google disabled and a zero request budget. No inbound port
or web server is needed. Review `doctor --json` and `providers --json` before use;
credential presence does not establish a working account or export entitlement.

Back up state while CLI processes are stopped: preserve `state.sqlite3` together
with its `gpx/` artifacts and `handoff/` files, plus private configuration in an
encrypted backup. A running database needs SQLite's backup API or a consistent
snapshot rather than copying the main file alone. HTTP caches are disposable.
SQLite schema migration retains original imports. A newer database version is
rejected rather than downgraded. Refreshing provider metadata invalidates its
cached export; original local imports remain immutable. Changed route revisions
receive distinct default handoff files and audit identities.

When upgrading from 0.3.0 or earlier, saved OSM geometry and cached OSM exports
require re-normalization before use. Run:

```bash
uv run --project /srv/outdoor-agent outdoor-agent get \
  --route-id osm:relation:YOUR_RELATION_ID --refresh --json
```

An unavailable provider leaves the legacy geometry quarantined; do not restore
the old artifact as a workaround. Original imported local GPX files remain
available. Search `coverage` counters distinguish unavailable geometry and capped
provider coverage from actual filter matches; zero results do not prove that no
gravel loop exists locally.

Install `skills/openclaw/outdoor-routes/SKILL.md` under the OpenClaw workspace's
`skills/outdoor-routes/` directory and set its fixed project path. Verify loading
on the target OpenClaw host; the repository validates the CLI and documented
skill format, not that separate runtime installation.

Example requests for the agent:

- “Find mapped hiking routes starting within 10 km of Utrecht, 5–15 km long.
  Show three with their original links and explain missing geometry or metrics.”
- “Save this legally exported GPX with its original source link. Check it for
  bicycle closures on 2026-10-11 and explain the uncertainty.”
- “Show a dry-run COROS handoff for this route. After I approve the path, prepare
  the GPX for manual app import; don't claim that my watch has received it.”
- “Recheck my saved routes for 2026-10-11 and report which findings changed.”

Run `recheck --date ... --strict --json` manually or from an operator-managed
scheduler. It downloads/parses each live feed once per batch, stores stable
comparisons and exits 6 on insufficient coverage. It does not send notifications
or run a daemon. Avoid logging full JSON reports in shared logs: they contain
route geometry and local paths. A private output file can be reviewed by the
agent. No timer or third-party notification integration is installed automatically.
