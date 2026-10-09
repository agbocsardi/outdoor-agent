# Development instructions

Implement incremental milestones in spec.md. Use uv for all Python tooling and execution.
Run `uv sync --dev`, `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy src`, and `uv lock --check` before a PR.
Keep unit/contract tests offline; no paid or credentialed calls in CI.
Retain source attribution, original GPX bytes, and explicit coverage uncertainty.
Never claim a provider capability without dated evidence. Account writes require approval.
JSON commands emit exactly one object; diagnostics go to stderr. Never log secrets or tracks.
