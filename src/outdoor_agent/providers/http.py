"""Bounded HTTPS I/O, redacted failures, throttling and private TTL caches."""

import asyncio
import fcntl
import hashlib
import ipaddress
import json
import os
import re
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from outdoor_agent.errors import OutdoorError

if TYPE_CHECKING:
    from outdoor_agent.config import Settings


def validate_endpoint(url: str) -> None:
    try:
        parsed = urlsplit(url)
        host = parsed.hostname.rstrip(".") if parsed.hostname else None
        valid = (
            parsed.scheme == "https"
            and host is not None
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
            and not parsed.fragment
            and not any(ord(c) < 33 for c in url)
            and "\\" not in url
        )
        if (
            not valid
            or host in {"localhost", "localhost.localdomain"}
            or (host and host.endswith((".local", ".internal", ".localhost")))
        ):
            raise ValueError("Unsafe endpoint")
        try:
            address = ipaddress.ip_address(host or "")
        except ValueError:
            if host and (
                "." not in host
                or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", part) for part in host.split("."))
            ):
                raise ValueError("Unsafe numeric or local hostname") from None
        else:
            if not address.is_global:
                raise ValueError("Endpoint must be public")
    except ValueError as exc:
        raise OutdoorError(
            "UNSAFE_URL", "Provider endpoint must be a public HTTPS URL without credentials."
        ) from exc


@dataclass(frozen=True)
class FetchResult:
    content: bytes
    fetched_at: datetime
    from_cache: bool
    stale: bool
    headers: dict[str, str]


class HTTPClient:
    def __init__(
        self, settings: "Settings", *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=False,
            transport=transport,
            headers={"User-Agent": settings.http_user_agent, "Accept-Encoding": "identity"},
        )

    async def __aenter__(self) -> "HTTPClient":
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.client.aclose()

    async def fetch(
        self,
        method: str,
        url: str,
        *,
        provider: str,
        params: dict[str, str | int | float] | None = None,
        data: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        max_bytes: int = 16 * 1024 * 1024,
        ttl: int = 0,
        cache_key: str | None = None,
        persist: bool = True,
        min_interval: float = 1.0,
        attempts: int = 3,
    ) -> FetchResult:
        validate_endpoint(url)
        if attempts not in (1, 2, 3):
            raise ValueError("HTTP retry attempts must be between one and three")
        key = (
            cache_key
            or hashlib.sha256(
                json.dumps([method, url, params, data, json_body], sort_keys=True).encode()
            ).hexdigest()
        )
        path = (
            self.settings.cache_dir
            / "http"
            / f"{hashlib.sha256((provider + key).encode()).hexdigest()}.json"
        )
        cached = self._read_cache(path, max_bytes) if ttl > 0 else None
        if cached and (datetime.now(UTC) - cached.fetched_at).total_seconds() < ttl:
            return cached
        if self.settings.offline:
            if cached:
                return FetchResult(cached.content, cached.fetched_at, True, True, cached.headers)
            raise OutdoorError(
                "OFFLINE_CACHE_MISS",
                "No cached response is available in offline mode.",
                4,
                provider=provider,
            )
        for attempt in range(attempts):
            if min_interval > 0:
                delay = await asyncio.to_thread(self._reserve_request, provider, min_interval)
                if delay > 0:
                    await asyncio.sleep(delay)
            try:
                async with self.client.stream(
                    method, url, params=params, data=data, json=json_body, headers=headers
                ) as response:
                    status = response.status_code
                    if status in (401, 403):
                        raise OutdoorError(
                            "AUTH_REQUIRED" if status == 401 else "PERMISSION_DENIED",
                            "Provider requires valid credentials or permission for this operation.",
                            3,
                            provider=provider,
                        )
                    if status == 429 or status >= 500:
                        if attempt < attempts - 1:
                            retry_after = response.headers.get("Retry-After", "")
                            delay = (
                                min(5.0, float(retry_after))
                                if retry_after.isdecimal()
                                else float(attempt + 1)
                            )
                            await asyncio.sleep(delay)
                            continue
                        raise OutdoorError(
                            "PROVIDER_RATE_LIMITED" if status == 429 else "PROVIDER_UNAVAILABLE",
                            "Provider temporarily unavailable; retry later.",
                            4,
                            provider=provider,
                            retryable=True,
                        )
                    if status >= 300:
                        raise OutdoorError(
                            "PROVIDER_REQUEST_FAILED",
                            f"Provider returned HTTP {status}; no redirect was followed.",
                            4,
                            provider=provider,
                        )
                    content = bytearray()
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding not in {"identity", "gzip", "deflate"}:
                        raise OutdoorError(
                            "PROVIDER_ENCODING_UNSUPPORTED",
                            "Provider returned unsupported HTTP compression.",
                            4,
                            provider=provider,
                        )
                    decoder = (
                        zlib.decompressobj(31 if encoding == "gzip" else 15)
                        if encoding != "identity" and not response.is_stream_consumed
                        else None
                    )
                    wire_bytes = 0
                    stream = (
                        response.aiter_bytes()
                        if response.is_stream_consumed
                        else response.aiter_raw()
                    )
                    try:
                        async for chunk in stream:
                            wire_bytes += len(chunk)
                            if wire_bytes > max_bytes:
                                raise OutdoorError(
                                    "PROVIDER_PAYLOAD_TOO_LARGE",
                                    "Provider response exceeds the configured payload limit.",
                                    4,
                                    provider=provider,
                                )
                            decoded = (
                                decoder.decompress(chunk, max_bytes - len(content) + 1)
                                if decoder
                                else chunk
                            )
                            if len(content) + len(decoded) > max_bytes:
                                raise OutdoorError(
                                    "PROVIDER_PAYLOAD_TOO_LARGE",
                                    "Provider response exceeds the configured payload limit.",
                                    4,
                                    provider=provider,
                                )
                            content.extend(decoded)
                        if decoder and not decoder.eof:
                            raise ValueError("Incomplete compressed payload")
                    except (zlib.error, ValueError) as exc:
                        raise OutdoorError(
                            "PROVIDER_ENCODING_INVALID",
                            "Provider compressed response is malformed.",
                            4,
                            provider=provider,
                        ) from exc
                    result = FetchResult(
                        bytes(content),
                        datetime.now(UTC),
                        False,
                        False,
                        {
                            k: response.headers[k]
                            for k in ("last-modified", "etag", "content-type")
                            if k in response.headers
                        },
                    )
                    if persist and ttl > 0:
                        self._write_cache(path, result)
                    return result
            except httpx.RequestError as exc:
                if attempt == attempts - 1:
                    raise OutdoorError(
                        "PROVIDER_UNAVAILABLE",
                        "Provider network request failed.",
                        4,
                        provider=provider,
                        retryable=True,
                    ) from exc
                await asyncio.sleep(attempt + 1)
        raise AssertionError("Retry loop exhausted")

    def _reserve_request(self, provider: str, interval: float) -> float:
        """Reserve a provider slot across CLI processes using a tiny private lock file."""
        path = (
            self.settings.cache_dir
            / "throttle"
            / f"{hashlib.sha256(provider.encode()).hexdigest()}.lock"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
            with os.fdopen(fd, "r+") as file:
                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
                try:
                    previous = float(file.read() or "0")
                except ValueError:
                    previous = 0.0
                current = time.time()
                reserved = max(current, previous + interval)
                file.seek(0)
                file.truncate()
                file.write(str(reserved))
                file.flush()
                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
                return max(0.0, reserved - current)
        except OSError as exc:
            raise OutdoorError(
                "THROTTLE_STATE_FAILED",
                "Cannot coordinate provider throttling; check cache permissions.",
                7,
                provider=provider,
            ) from exc

    def _read_cache(self, path: Path, max_bytes: int) -> FetchResult | None:
        import base64

        try:
            if path.stat().st_size > max_bytes * 2 + 8192:
                return None
            value = json.loads(path.read_text())
            if (
                not isinstance(value, dict)
                or not isinstance(value.get("headers"), dict)
                or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in value["headers"].items()
                )
            ):
                return None
            content = base64.b64decode(value["content"], validate=True)
            if len(content) > max_bytes:
                return None
            fetched = datetime.fromisoformat(value["fetched_at"])
            if fetched.tzinfo is None or (fetched - datetime.now(UTC)).total_seconds() > 60:
                return None
            return FetchResult(content, fetched, True, False, value["headers"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _write_cache(self, path: Path, result: FetchResult) -> None:
        import base64
        import tempfile

        try:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary: str | None = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
                    temporary = file.name
                    json.dump(
                        {
                            "content": base64.b64encode(result.content).decode(),
                            "fetched_at": result.fetched_at.isoformat(),
                            "headers": result.headers,
                        },
                        file,
                    )
                os.replace(temporary, path)
                temporary = None
            finally:
                if temporary:
                    Path(temporary).unlink(missing_ok=True)
        except OSError as exc:
            raise OutdoorError(
                "CACHE_WRITE_FAILED",
                "Cannot write provider cache; check directory permissions.",
                7,
                retryable=False,
            ) from exc
