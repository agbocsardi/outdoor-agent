import json

from outdoor_agent.models.privacy import (
    MAX_METADATA_BYTES,
    MAX_METADATA_FIELDS,
    MAX_METADATA_VALUE_CHARS,
    scrub_metadata,
)
from outdoor_agent.models.routes import Route


def test_provider_metadata_removes_credentials_and_contact_details() -> None:
    raw = {
        "surface": "gravel",
        "authorization": "Bearer private",
        "api_key": "private",
        "creator_email": "person@example.test",
        "note": "Contact person@example.test",
        "other": "token=private",
        "website": "https://user:password@example.test/path?token=private#private",
    }
    clean = scrub_metadata(raw)
    assert clean == {"surface": "gravel", "website": "https://example.test/path"}
    route = Route(
        id="local:fixture",
        provider="local",
        provider_route_id="fixture",
        name="Fixture",
        attribution="Fixture",
        raw_metadata=raw,
    )
    serialized = route.model_dump_json()
    assert "private" not in serialized
    assert "person@" not in serialized
    assert "metadata was bounded or redacted" in route.warnings[0]
    assert raw["authorization"] == "Bearer private"


def test_provider_metadata_has_total_and_per_field_bounds() -> None:
    clean = scrub_metadata({f"key{i:03d}": "é" * 5000 for i in range(1000)})
    assert len(clean) <= MAX_METADATA_FIELDS
    assert len(json.dumps(clean, ensure_ascii=False).encode()) <= MAX_METADATA_BYTES
    assert all(len(str(value)) <= MAX_METADATA_VALUE_CHARS for value in clean.values())
    assert scrub_metadata(clean) == clean


def test_invalid_metadata_urls_are_omitted() -> None:
    assert (
        scrub_metadata(
            {"a": "file:///private", "b": "https://example.test:bad/", "website": "not a URL"}
        )
        == {}
    )
