"""Google Places API v1 request constants for opt-in near-route discovery."""

from typing import Final

ENDPOINT: Final = "https://places.googleapis.com/v1/places:searchNearby"
FIELD_MASK: Final = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.googleMapsUri,places.types"
)
RADIUS_METERS: Final = 500.0
MAX_RESULTS: Final = 10

# These are semantic inputs supported by the service. `viewpoint` deliberately maps to
# tourist_attraction because Places does not expose a stable viewpoint type.
CATEGORY_TYPES: Final[dict[str, str]] = {
    "viewpoint": "tourist_attraction",
    "attraction": "tourist_attraction",
    "trailhead": "hiking_area",
    "park": "park",
    "campground": "campground",
    "picnic": "picnic_ground",
}

FIELD_MASK_HEADER: Final = "X-Goog-FieldMask"
API_KEY_HEADER: Final = "X-Goog-Api-Key"
