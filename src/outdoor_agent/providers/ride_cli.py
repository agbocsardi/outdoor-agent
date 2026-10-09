"""Authenticated Ride with GPS transport through the locally logged-in ``ride`` CLI.

The CLI owns credentials and refresh, so outdoor-agent never reads, copies or logs tokens.
Only bounded stdout is returned; stderr is inspected for a coarse error class and discarded.
"""

import asyncio
import re
from datetime import UTC, datetime
from shutil import which

from outdoor_agent.config import Settings
from outdoor_agent.errors import OutdoorError
from outdoor_agent.providers.http import FetchResult

_STDERR_LIMIT = 64 * 1024
_CHUNK = 64 * 1024
_PATH = re.compile(r"^/api/v1/[A-Za-z0-9][A-Za-z0-9._/-]*$")

_AUTH_REQUIRED_MARKERS = (
    "not logged in",
    "logged out",
    "log in",
    "login",
    "authenticate",
    "sign in",
)
_AUTH_EXPIRED_MARKERS = ("unauthorized", "401", "expired", "token")
_PERMISSION_MARKERS = ("forbidden", "403", "permission")
_NOT_FOUND_MARKERS = ("not found", "cannot be found")


def _has_control(value: str) -> bool:
    return any(ord(character) < 32 or ord(character) == 127 for character in value)


def _auth_required() -> OutdoorError:
    return OutdoorError(
        "AUTH_REQUIRED",
        "Ride with GPS requires an authenticated ride CLI or configured credentials.",
        3,
        provider="ridewithgps",
        suggestions=[
            "Run `ride login` to authenticate the installed ride CLI.",
            "Or configure RIDEWITHGPS_API_KEY and RIDEWITHGPS_AUTH_TOKEN securely.",
        ],
    )


def _failure(stderr: bytes) -> OutdoorError:
    """Classify a failed run from redacted stderr markers; raw output is never echoed."""
    text = stderr.decode("utf-8", "replace").lower()
    if any(marker in text for marker in _AUTH_REQUIRED_MARKERS):
        return _auth_required()
    if any(marker in text for marker in _AUTH_EXPIRED_MARKERS):
        return OutdoorError(
            "AUTH_EXPIRED",
            "Ride CLI credentials were rejected; re-authenticate securely.",
            3,
            provider="ridewithgps",
            suggestions=["Run `ride login` again."],
        )
    if any(marker in text for marker in _PERMISSION_MARKERS):
        return OutdoorError(
            "PERMISSION_DENIED",
            "Ride CLI account is not permitted to read this resource.",
            3,
            provider="ridewithgps",
        )
    if any(marker in text for marker in _NOT_FOUND_MARKERS):
        return OutdoorError(
            "PROVIDER_NOT_FOUND",
            "Ride with GPS does not expose this resource.",
            4,
            provider="ridewithgps",
        )
    return OutdoorError(
        "PROVIDER_UNAVAILABLE",
        "Ride CLI failed without a recognized diagnostic.",
        4,
        provider="ridewithgps",
        retryable=True,
    )


async def _terminate(process: "asyncio.subprocess.Process") -> None:
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        await process.wait()
    except ProcessLookupError:
        pass


class RideCLI:
    """Run ``ride api get`` as a bounded, shell-free subprocess."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def executable(self) -> str | None:
        if self.settings.ride_cli_path:
            return self.settings.ride_cli_path
        return which("ride")

    async def fetch(
        self, path: str, *, query: dict[str, str] | None = None, max_bytes: int
    ) -> FetchResult:
        executable = self.executable()
        if executable is None:
            raise _auth_required()
        self._validate(path, query)
        argv = [executable, "api", "get", path]
        for key, value in (query or {}).items():
            argv += ["-q", f"{key}={value}"]
        stdout, stderr, returncode = await self._run(argv, max_bytes)
        if returncode != 0:
            raise _failure(stderr)
        return FetchResult(
            content=stdout,
            fetched_at=datetime.now(UTC),
            from_cache=False,
            stale=False,
            headers={},
        )

    @staticmethod
    def _validate(path: str, query: dict[str, str] | None) -> None:
        if not _PATH.fullmatch(path) or ".." in path:
            raise OutdoorError(
                "INVALID_ARGUMENTS",
                "Ride CLI path must be a bounded /api/v1/ resource path.",
                2,
                provider="ridewithgps",
            )
        for key, value in (query or {}).items():
            if not key or "=" in key or _has_control(key) or _has_control(value):
                raise OutdoorError(
                    "INVALID_ARGUMENTS",
                    "Ride CLI query parameters must be simple key/value pairs.",
                    2,
                    provider="ridewithgps",
                )

    async def _run(self, argv: list[str], max_bytes: int) -> tuple[bytes, bytes, int]:
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise _auth_required() from exc
        stdout = bytearray()
        stderr = bytearray()
        overflow = False

        async def drain(
            stream: "asyncio.StreamReader | None", buffer: bytearray, limit: int
        ) -> None:
            nonlocal overflow
            assert stream is not None
            while chunk := await stream.read(_CHUNK):
                buffer.extend(chunk)
                if len(buffer) > limit:
                    overflow = True
                    if process.returncode is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                    return

        readers = [
            asyncio.create_task(drain(process.stdout, stdout, max_bytes)),
            asyncio.create_task(drain(process.stderr, stderr, _STDERR_LIMIT)),
        ]
        try:
            async with asyncio.timeout(self.settings.http_timeout_seconds):
                await asyncio.gather(*readers)
                await process.wait()
        except TimeoutError as exc:
            await _terminate(process)
            raise OutdoorError(
                "PROVIDER_TIMEOUT",
                "Ride CLI did not answer within the configured timeout.",
                4,
                provider="ridewithgps",
                retryable=True,
            ) from exc
        finally:
            for reader in readers:
                reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            if process.returncode is None:
                await _terminate(process)
        if overflow:
            raise OutdoorError(
                "PROVIDER_PAYLOAD_TOO_LARGE",
                "Ride CLI response exceeds the configured payload limit.",
                4,
                provider="ridewithgps",
            )
        return bytes(stdout), bytes(stderr), process.returncode or 0
