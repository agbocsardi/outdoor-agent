"""Offline contract tests for bounded provider HTTP and its private response cache."""

import asyncio
import base64
import gzip
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.providers.http import HTTPClient


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        for offset in range(0, len(self.body), 7):
            yield self.body[offset : offset + 7]


@pytest.mark.asyncio
async def test_http_gzip_is_bounded_after_decompression(tmp_path: Path) -> None:
    async with HTTPClient(
        make_settings(tmp_path),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                stream=ChunkedBody(gzip.compress(b"x" * 1000)),
                headers={"Content-Encoding": "gzip"},
            )
        ),
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET", "https://example.com/data", provider="gzip", max_bytes=100, min_interval=0
            )
    assert error.value.code == "PROVIDER_PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_http_gzip_stream_decodes_and_rejects_truncation(tmp_path: Path) -> None:
    body = gzip.compress(b"valid response")
    for payload, valid in [(body, True), (body[:-4], False)]:
        async with HTTPClient(
            make_settings(tmp_path),
            transport=httpx.MockTransport(
                lambda request, payload=payload: httpx.Response(
                    200,
                    stream=ChunkedBody(payload),
                    headers={"Content-Encoding": "gzip"},
                )
            ),
        ) as client:
            if valid:
                result = await client.fetch(
                    "GET", "https://example.com/data", provider="gzip", min_interval=0
                )
                assert result.content == b"valid response"
            else:
                with pytest.raises(OutdoorError) as error:
                    await client.fetch(
                        "GET", "https://example.com/data", provider="gzip", min_interval=0
                    )
                assert error.value.code == "PROVIDER_ENCODING_INVALID"


def make_settings(tmp_path: Path, *, offline: bool = False) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        cache_dir=tmp_path / "cache",
        offline=offline,
    )


def endpoint_cache_path(settings: Settings, *, provider: str = "test-provider") -> Path:
    key = hashlib.sha256(
        json.dumps(["GET", "https://example.com/data", None, None, None], sort_keys=True).encode()
    ).hexdigest()
    filename = hashlib.sha256((provider + key).encode()).hexdigest() + ".json"
    return settings.cache_dir / "http" / filename


@pytest.mark.asyncio
async def test_success_streams_payload_within_limit_and_returns_safe_headers(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"small",
            headers={
                "Content-Type": "application/octet-stream",
                "ETag": "etag-1",
                "X-Secret": "omit",
            },
        )

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch(
            "GET", "https://example.com/data", provider="test-provider", min_interval=0
        )
    assert result.content == b"small"
    assert result.from_cache is False
    assert result.stale is False
    assert result.headers == {"etag": "etag-1", "content-type": "application/octet-stream"}
    assert result.fetched_at.tzinfo is not None


@pytest.mark.asyncio
async def test_streaming_payload_limit_stops_oversized_body(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=b"0123456789")

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET",
                "https://example.com/data",
                provider="test-provider",
                max_bytes=5,
                min_interval=0,
                attempts=1,
            )
    assert requests == 1
    assert error.value.code == "PROVIDER_PAYLOAD_TOO_LARGE"


@pytest.mark.asyncio
async def test_redirect_is_not_followed(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"Location": "https://other.example/secret"})

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET", "https://example.com/data", provider="test-provider", min_interval=0
            )
    assert len(requests) == 1
    assert error.value.code == "PROVIDER_REQUEST_FAILED"
    assert "other.example" not in error.value.message


@pytest.mark.parametrize(
    ("status", "code"),
    [(401, "AUTH_REQUIRED"), (403, "PERMISSION_DENIED")],
)
@pytest.mark.asyncio
async def test_auth_errors_are_redacted(tmp_path: Path, status: int, code: str) -> None:
    secret = "API_SECRET_SENTINEL"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"message": secret})

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET",
                "https://example.com/data?token=" + secret,
                provider="test-provider",
                min_interval=0,
                attempts=1,
            )
    assert error.value.code == code
    assert secret not in error.value.message
    assert secret not in str(error.value)


@pytest.mark.parametrize("status", [429, 500])
@pytest.mark.asyncio
async def test_retryable_statuses_retry_up_to_attempt_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: int
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            return httpx.Response(status, headers={"Retry-After": "8"})
        return httpx.Response(200, content=b"recovered")

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch(
            "GET",
            "https://example.com/data",
            provider=f"retry-{status}",
            min_interval=0,
            attempts=3,
        )
    assert calls == 3
    assert result.content == b"recovered"
    assert sleeps == [5.0, 5.0]


@pytest.mark.asyncio
async def test_attempts_one_caps_billable_http_sends(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429)

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET",
                "https://example.com/data",
                provider="one-send",
                min_interval=0,
                attempts=1,
            )
    assert calls == 1
    assert error.value.code == "PROVIDER_RATE_LIMITED"
    assert error.value.retryable is True


@pytest.mark.asyncio
async def test_network_errors_are_redacted_and_retry_budget_is_honored(
    tmp_path: Path,
) -> None:
    calls = 0
    secret = "NETWORK_SECRET_SENTINEL"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectError(secret, request=request)

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET",
                "https://example.com/data?token=" + secret,
                provider="network-error",
                min_interval=0,
                attempts=1,
            )
    assert calls == 1
    assert error.value.code == "PROVIDER_UNAVAILABLE"
    assert error.value.retryable is True
    assert secret not in error.value.message


@pytest.mark.asyncio
async def test_network_error_retries_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, content=b"recovered")

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        result = await client.fetch(
            "GET",
            "https://example.com/data",
            provider="network-retry",
            min_interval=0,
            attempts=2,
        )
    assert calls == 2
    assert sleeps == [1]
    assert result.content == b"recovered"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/data",
        "https://localhost/data",
        "https://127.0.0.1/data",
        "https://[::1]/data",
        "https://10.1.2.3/data",
        "https://example.local/data",
        "https://user:password@example.com/data",
        "https://example.com:444/data",
        "https://example.com/data#fragment",
        "https://12345/data",
        "https://localhost./data",
        "https://127.1/data",
        "https://0177.0.0.1/data",
        "https://0x7f.0.0.1/data",
    ],
)
@pytest.mark.asyncio
async def test_unsafe_endpoint_is_rejected_before_transport(tmp_path: Path, url: str) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200)

    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch("GET", url, provider="unsafe", min_interval=0)
    assert error.value.code == "UNSAFE_URL"
    assert requests == 0


@pytest.mark.asyncio
async def test_attempt_parameter_rejects_invalid_values(tmp_path: Path) -> None:
    async with HTTPClient(
        make_settings(tmp_path), transport=httpx.MockTransport(lambda req: httpx.Response(200))
    ) as client:
        with pytest.raises(ValueError, match="retry attempts"):
            await client.fetch(
                "GET",
                "https://example.com/data",
                provider="bad-attempts",
                attempts=4,
            )


@pytest.mark.asyncio
async def test_ttl_cache_is_private_and_used_until_expiration(tmp_path: Path) -> None:
    calls = 0
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"cached-content", headers={"ETag": "v1"})

    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        first = await client.fetch(
            "GET", "https://example.com/data", provider="cache-test", ttl=60, min_interval=0
        )
        second = await client.fetch(
            "GET", "https://example.com/data", provider="cache-test", ttl=60, min_interval=0
        )
    cache_dir = settings.cache_dir / "http"
    assert calls == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.stale is False
    assert second.content == b"cached-content"
    assert list(cache_dir.glob("*.json"))
    assert cache_dir.stat().st_mode & 0o777 == 0o700
    assert b"https://example.com/data" not in next(cache_dir.glob("*.json")).read_bytes()


@pytest.mark.asyncio
async def test_ttl_zero_and_persist_false_do_not_write_cache(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"private")

    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        await client.fetch(
            "GET",
            "https://example.com/data",
            provider="uncached",
            ttl=60,
            persist=False,
            min_interval=0,
        )
        await client.fetch(
            "GET", "https://example.com/data", provider="zero-ttl", ttl=0, min_interval=0
        )
    assert not (settings.cache_dir / "http").exists()


@pytest.mark.asyncio
async def test_offline_uses_fresh_cache_without_transport(tmp_path: Path) -> None:
    online_settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"cached", headers={"ETag": "cached-tag"})

    async with HTTPClient(online_settings, transport=httpx.MockTransport(handler)) as online:
        await online.fetch(
            "GET", "https://example.com/data", provider="offline-cache", ttl=120, min_interval=0
        )

    def should_not_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline cache hit must not open transport")

    async with HTTPClient(
        make_settings(tmp_path, offline=True), transport=httpx.MockTransport(should_not_call)
    ) as offline:
        result = await offline.fetch(
            "GET", "https://example.com/data", provider="offline-cache", ttl=120, min_interval=0
        )
    assert result.content == b"cached"
    assert result.from_cache is True
    assert result.stale is False
    assert result.headers == {"etag": "cached-tag"}


@pytest.mark.asyncio
async def test_offline_uses_stale_cache_and_marks_it_stale(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"old", headers={"ETag": "old-tag"})

    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as online:
        await online.fetch(
            "GET", "https://example.com/data", provider="stale-cache", ttl=1, min_interval=0
        )
    cache_path = endpoint_cache_path(settings, provider="stale-cache")
    cache = json.loads(cache_path.read_text())
    cache["fetched_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    cache_path.write_text(json.dumps(cache))

    def should_not_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline stale cache must not open transport")

    async with HTTPClient(
        make_settings(tmp_path, offline=True), transport=httpx.MockTransport(should_not_call)
    ) as offline:
        result = await offline.fetch(
            "GET", "https://example.com/data", provider="stale-cache", ttl=1, min_interval=0
        )
    assert result.content == b"old"
    assert result.from_cache is True
    assert result.stale is True


@pytest.mark.asyncio
async def test_offline_cache_miss_is_typed(tmp_path: Path) -> None:
    def should_not_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError("offline cache miss must not open transport")

    async with HTTPClient(
        make_settings(tmp_path, offline=True), transport=httpx.MockTransport(should_not_call)
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET", "https://example.com/data", provider="empty-cache", ttl=60, min_interval=0
            )
    assert error.value.code == "OFFLINE_CACHE_MISS"
    assert error.value.exit_code == 4


@pytest.mark.parametrize(
    "cache_document",
    [
        b"not-json",
        json.dumps(
            {"content": "!bad-base64!", "fetched_at": "2026-01-01T00:00:00+00:00", "headers": {}}
        ).encode(),
        json.dumps(
            {"content": base64.b64encode(b"abc").decode(), "fetched_at": "no-date", "headers": {}}
        ).encode(),
        json.dumps(
            {
                "content": base64.b64encode(b"abc").decode(),
                "fetched_at": "2026-01-01T00:00:00",
                "headers": {},
            }
        ).encode(),
        json.dumps(
            {
                "content": base64.b64encode(b"abc").decode(),
                "fetched_at": "2026-01-01T00:00:00+00:00",
            }
        ).encode(),
        json.dumps(
            {
                "content": base64.b64encode(b"abc").decode(),
                "fetched_at": "2026-01-01T00:00:00+00:00",
                "headers": ["not", "a", "mapping"],
            }
        ).encode(),
    ],
)
@pytest.mark.asyncio
async def test_malformed_cache_is_ignored_offline_as_a_cache_miss(
    tmp_path: Path, cache_document: bytes
) -> None:
    settings = make_settings(tmp_path, offline=True)
    cache_path = endpoint_cache_path(settings, provider="malformed")
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(cache_document)

    async with HTTPClient(
        settings,
        transport=httpx.MockTransport(
            lambda request: (_ for _ in ()).throw(AssertionError("offline mode used transport"))
        ),
    ) as client:
        with pytest.raises(OutdoorError) as error:
            await client.fetch(
                "GET", "https://example.com/data", provider="malformed", ttl=60, min_interval=0
            )
    assert error.value.code == "OFFLINE_CACHE_MISS"


@pytest.mark.asyncio
async def test_stale_cache_refreshes_online(tmp_path: Path) -> None:
    calls = 0
    settings = make_settings(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"new")

    async with HTTPClient(settings, transport=httpx.MockTransport(handler)) as client:
        await client.fetch(
            "GET", "https://example.com/data", provider="refresh-cache", ttl=60, min_interval=0
        )
        cache_path = endpoint_cache_path(settings, provider="refresh-cache")
        cache = json.loads(cache_path.read_text())
        cache["fetched_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        cache_path.write_text(json.dumps(cache))
        result = await client.fetch(
            "GET", "https://example.com/data", provider="refresh-cache", ttl=60, min_interval=0
        )
    assert calls == 2
    assert result.content == b"new"
    assert result.from_cache is False
    assert result.stale is False
