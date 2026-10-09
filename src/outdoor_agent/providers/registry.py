"""Dated evidence and honest runtime availability; documentation isn't an adapter."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from outdoor_agent.models.routes import Activity, Model


class CapabilityState(StrEnum):
    supported = "supported"
    unsupported = "unsupported"
    unverified = "unverified"
    restricted = "restricted"
    degraded = "degraded"


class Capability(Model):
    state: CapabilityState
    checked_at: datetime | None = None
    evidence: list[str] = Field(default_factory=list)
    notes: str


class Provider(Model):
    provider: str
    implemented: bool
    enabled: bool = True
    auth_required: bool
    activities: list[Activity]
    coverage: str
    capabilities: dict[str, Capability]
    notes: str


CHECKED = datetime.fromisoformat("2026-10-09T14:51:49+00:00")


def capability(state: str, notes: str, *evidence: str) -> Capability:
    return Capability(
        state=CapabilityState(state),
        checked_at=CHECKED if evidence else None,
        evidence=list(evidence),
        notes=notes,
    )


def registry() -> list[Provider]:
    """Return support evidence independently of implementation availability."""
    return [
        Provider(
            provider="local",
            implemented=True,
            auth_required=False,
            activities=list(Activity),
            coverage="Saved/imported route records in this user's private catalog; no closure coverage.",
            notes="No network calls. User-supplied community provenance is not independently verified.",
            capabilities={
                "inspect_gpx": capability("supported", "Bounded local parsing, tested offline."),
                "import_gpx": capability(
                    "supported", "Original bytes and provenance cached locally."
                ),
                "discover_public": capability(
                    "unsupported", "Local files are not a community catalog."
                ),
                "discover_saved": capability(
                    "supported",
                    "Searches only records explicitly imported or saved in the user's private RouteStore; no fixture scanning or public catalog access.",
                ),
            },
        ),
        Provider(
            provider="ridewithgps",
            implemented=True,
            auth_required=True,
            activities=[],
            coverage="Authenticated account routes; public community discovery not verified.",
            notes="Documented authenticated reads and authorized GPX export implemented; account credentials not probed in this build. Public community discovery and remote writes remain unavailable.",
            capabilities={
                "discover_public": capability(
                    "unsupported",
                    "Documented route list is scoped to the authenticated user, not public search.",
                    "https://ridewithgps.com/api/v1/doc",
                    "https://github.com/ridewithgps/ride-cli",
                ),
                "get_metadata": capability(
                    "restricted",
                    "Requires authorized account; unauthenticated probe returned 401.",
                    "https://ridewithgps.com/api/v1/doc",
                ),
                "download_gpx": capability(
                    "restricted",
                    "Authentication, route visibility, entitlement and permissions must be verified per route.",
                    "https://ridewithgps.com/api/v1/doc",
                ),
                "pin_route": capability(
                    "unverified",
                    "No authenticated supported write tested.",
                    "https://support.ridewithgps.com/hc/en-us/articles/27231998487963-COROS",
                ),
            },
        ),
        Provider(
            provider="osm",
            implemented=True,
            auth_required=False,
            activities=[
                Activity.hiking,
                Activity.walking,
                Activity.running,
                Activity.trail_running,
                Activity.road,
                Activity.gravel,
                Activity.mtb,
            ],
            coverage="Mapped official route relations worldwide; completeness varies.",
            notes="Read-only Overpass and cached Nominatim adapters implemented; mapped trails are official_trail, not community recommendations.",
            capabilities={
                "discover_public": capability(
                    "supported",
                    "Bounded live Overpass query returned 22 real relation records on 2026-10-09; activity filtering and reconstruction tested.",
                    "https://wiki.openstreetmap.org/wiki/Overpass_API",
                ),
                "get_metadata": capability(
                    "supported",
                    "Bounded relation reads implemented; OSM attribution and ODbL retained.",
                    "https://wiki.openstreetmap.org/wiki/Overpass_API",
                ),
                "check_restrictions": capability(
                    "degraded",
                    "A bounded read-only Overpass probe returned one live snapshot with 599 restriction events on 2026-10-09. Tag timing and trail-closure coverage remain incomplete and unknown.",
                    "https://wiki.openstreetmap.org/wiki/Overpass_API",
                ),
            },
        ),
        Provider(
            provider="ndw",
            implemented=True,
            auth_required=False,
            activities=[Activity.road, Activity.gravel, Activity.mtb],
            coverage="Netherlands roads; bike/foot restrictions require explicit event evidence. Trails incomplete.",
            notes="Current/planned DATEX II v3 feeds and direct GML decoded; unresolved references, road-only coverage and timing uncertainty are explicit.",
            capabilities={
                "fetch_closures": capability(
                    "supported",
                    "Public read-only feed probe returned 200; this is feed access, not closure checking.",
                    "https://opendata.ndw.nu/tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz",
                ),
                "decode_locations": capability(
                    "degraded",
                    "Direct WGS84 GML decoded and tested. Alert-C references are retained unresolved; reliable refresh cadence is not established.",
                    "https://docs.ndw.nu/",
                ),
                "check_closures": capability(
                    "degraded",
                    "Mode/date-aware metric assessments implemented; unresolved Alert-C and incomplete trail/future coverage can prevent a conclusion.",
                ),
            },
        ),
        Provider(
            provider="wikiloc",
            implemented=False,
            auth_required=True,
            activities=[],
            coverage="User-supplied original URL/GPX only; no scraping or export entitlement bypass.",
            notes="Import local GPX with --source-url; no browser scraping or entitlement bypass.",
            capabilities={
                "discover_public": capability(
                    "unverified", "No supported public structured API established."
                ),
                "download_gpx": capability(
                    "restricted",
                    "User must obtain GPX through entitled official export.",
                    "https://help.wikiloc.com/",
                ),
            },
        ),
        Provider(
            provider="google",
            implemented=True,
            auth_required=True,
            activities=[],
            coverage="Places enrichment only; never a closure feed.",
            notes="Opt-in Nearby Search adapter with a hard per-run request budget, required independent API key, and no Places response persistence. No paid live probe performed.",
            capabilities={
                "places": capability(
                    "restricted",
                    "Documented Nearby Search implemented and tested with mock contracts; explicit opt-in, key and quota required.",
                    "https://developers.google.com/maps/documentation/places/web-service/overview",
                )
            },
        ),
        Provider(
            provider="coros",
            implemented=True,
            auth_required=True,
            activities=[],
            coverage="Manual GPX app handoff documented; physical watch synchronization unverified.",
            notes="Default dry-run; confirmed local GPX preparation and manual official app instructions. No third-party account writes or claimed watch delivery.",
            capabilities={
                "manual_gpx_import": capability(
                    "supported",
                    "Official COROS app import flow; user performs import and watch sync.",
                    "https://support.coros.com/hc/en-us/articles/360040243352-Troubleshooting-GPX-Route-Imports-for-Breadcrumb-Navigation",
                ),
                "automated_delivery": capability(
                    "unverified", "No authenticated supported API write/visibility probe performed."
                ),
                "partner_route_push": capability(
                    "restricted",
                    "Official partner API advertises route/push; company/platform application and OAuth credentials required. No authenticated push tested.",
                    "https://support.coros.com/hc/en-us/articles/53181766856724-Partner-API-Access",
                ),
                "mcp_gpx_import": capability(
                    "unsupported",
                    "Official self-service MCP explicitly excludes GPX route import/export.",
                    "https://support.coros.com/hc/en-us/articles/53181619102996-Build-on-COROS-MCP",
                ),
                "watch_sync": capability(
                    "unsupported", "CLI cannot verify delivery to a physical watch."
                ),
            },
        ),
    ]
