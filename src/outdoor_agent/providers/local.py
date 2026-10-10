"""Discovery adapter over routes explicitly stored in the user's private catalog."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from outdoor_agent.errors import OutdoorError
from outdoor_agent.geo.geometry import within_bbox
from outdoor_agent.models.routes import Activity, SourceType
from outdoor_agent.providers.base import SearchCriteria, SearchResult
from outdoor_agent.providers.osm import activity_matches
from outdoor_agent.providers.registry import Provider, registry
from outdoor_agent.storage.db import RouteStore

if TYPE_CHECKING:
    from outdoor_agent.config import Settings

MAX_LOCAL_RECORDS = 500
PROVENANCE_WARNING = "User-supplied community provenance; authorship and whether the route was ridden were not independently verified."


class LocalCatalogProvider:
    """Search only saved RouteStore records; never scans fixtures or source folders."""

    def __init__(
        self,
        settings: Settings,
        *,
        source_type: Literal["community", "official_trail", "all"] = "all",
    ) -> None:
        self.settings = settings
        self.source_type = source_type
        self.declaration: Provider = next(item for item in registry() if item.provider == "local")

    async def search(self, criteria: SearchCriteria) -> SearchResult:
        records = RouteStore(self.settings.data_dir).list_routes(limit=MAX_LOCAL_RECORDS)
        if not records:
            raise _empty_catalog()
        eligible_types = {
            "community": {SourceType.community},
            "official_trail": {SourceType.official_trail},
            "all": {SourceType.community, SourceType.official_trail, SourceType.unknown},
        }[self.source_type]
        typed_records = [route for route in records if route.source_type in eligible_types]
        if not typed_records:
            raise _empty_catalog()
        bounds = criteria.bbox
        selected = [
            route
            for route in typed_records
            if _activity_compatible(criteria.activity, route.activity)
            and within_bbox(route, bounds)
        ]
        warnings = []
        if len(selected) > criteria.limit:
            warnings.append(
                "Local catalog candidate limit reached; additional matching routes may be omitted."
            )
        if len(records) >= MAX_LOCAL_RECORDS:
            warnings.append(
                f"Local route search examined at most {MAX_LOCAL_RECORDS} saved records; additional records may be omitted."
            )
        for route in selected:
            if (
                route.source_type == SourceType.community
                and PROVENANCE_WARNING not in route.warnings
            ):
                route.warnings.append(PROVENANCE_WARNING)
        return SearchResult(routes=selected[: criteria.limit], warnings=warnings)


def _empty_catalog() -> OutdoorError:
    return OutdoorError(
        "LOCAL_CATALOG_EMPTY",
        "No saved routes match the selected source type in the local catalog. Import an entitled GPX with --source-type community --attribution to search it.",
        2,
        provider="local",
        suggestions=[
            "Import only a GPX you may lawfully store, and record its source URL and creator attribution when known."
        ],
    )


def _activity_compatible(requested: Activity, actual: Activity) -> bool:
    if requested in {Activity.walking, Activity.hiking} and actual in {
        Activity.walking,
        Activity.hiking,
    }:
        return True
    return activity_matches(requested, actual)
