# Google Places near-route enrichment

Google Places is an optional, quota-controlled enrichment. It is disabled by default and
does not provide route, trail-condition, or closure evidence. Enable it in
`[outdoor_agent]` configuration with `google_enabled = true`, set
`max_google_requests_per_run` to a deliberate limit, and provide `GOOGLE_MAPS_API_KEY`
through the process environment. The key is excluded from configuration representations and
is never included in results or diagnostics.

Call `outdoor_agent.services.places.enrich_places(settings, route, category)` with one of
`viewpoint`, `attraction`, `trailhead`, `park`, `campground`, or `picnic`. It issues Google
Places API (New) Nearby Search requests at up to 20 evenly spaced WGS84 route samples, capped
by the configured per-run budget. Automatic transport retries are disabled so each budget
unit permits at most one HTTP send. Each search covers a 500 metre circle and requests at most
10 results. Only sampled point centers are transmitted; the full route is not sent. Results
are deduplicated by Google place ID and filtered using WGS84 geodesic distance from each
sample center. A `viewpoint` query maps to Google's `tourist_attraction` type because a stable
viewpoint type is not exposed; the result includes this uncertainty as a warning.

The adapter requests only place ID, display name, formatted address, location, Maps URI, and
place types. It does not request ratings, reviews, or photos. Responses are not cached or
persisted, including when `places_cache_ttl_seconds` is configured, because retention rights
for Google content have not been established here. Offline mode never makes a network call and
returns an empty partial result with a warning and zero requests.
Provider errors leave a partial result with a warning; disabled capability, missing key,
unsupported category, and a zero request budget are typed `OutdoorError` failures.

Nearby Search calls may be billable under the Google Maps Platform account's current pricing
and quota rules. This adapter enforces a per-invocation request budget and small response
limits and one HTTP send per budget unit, but account-level quotas and billing controls remain
Google's responsibility. Review
the current [Places API policies](https://developers.google.com/maps/documentation/places/web-service/policies)
and [pricing](https://developers.google.com/maps/billing-and-pricing/pricing) before enabling
it. Google Maps attribution and the returned Google Maps URI are retained with each result.
