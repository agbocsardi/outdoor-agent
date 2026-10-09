"""GPX export adapter for existing cached source geometry."""

import hashlib
import tempfile

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.export import route_gpx
from outdoor_agent.models.routes import GPXArtifact, Route
from outdoor_agent.providers.registry import Provider, registry


class CachedGeometryExporter:
    """Encode a cached route without claiming that it is original provider GPX."""

    def __init__(self, route: Route) -> None:
        self.route = route
        self.declaration: Provider = next(
            item for item in registry() if item.provider == route.provider
        )

    async def export_gpx(self, route_id: str) -> GPXArtifact:
        if route_id != self.route.id:
            raise OutdoorError("INVALID_ROUTE_ID", "Route ID does not match cached route.")
        return _temporary_artifact(self.route.id, route_gpx(self.route))


def _temporary_artifact(route_id: str, content: bytes) -> GPXArtifact:
    try:
        with tempfile.NamedTemporaryFile(
            prefix="outdoor-agent-", suffix=".gpx", delete=False
        ) as file:
            file.write(content)
            file.flush()
            return GPXArtifact(
                route_id=route_id,
                path=file.name,
                sha256=hashlib.sha256(content).hexdigest(),
                byte_count=len(content),
            )
    except OSError as exc:
        raise OutdoorError("EXPORT_WRITE_FAILED", "Cannot stage GPX artifact locally.", 7) from exc
