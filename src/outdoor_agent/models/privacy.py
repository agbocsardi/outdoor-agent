"""Bounded provider metadata; credentials/contact details never become cached extras."""

import json
import re
from urllib.parse import urlsplit, urlunsplit

MAX_METADATA_BYTES = 4096
MAX_METADATA_FIELDS = 32
MAX_METADATA_VALUE_CHARS = 512
Scalar = str | int | float | bool | None
_SENSITIVE = re.compile(
    r"token|password|secret|authorization|cookie|session|api.?key|email|phone", re.I
)
_SECRET_VALUE = re.compile(
    r"(?:bearer\s+\S+|(?:password|token|api.?key|secret)\s*[:=]\s*\S+)",
    re.I,
)


def scrub_metadata(values: dict[str, Scalar]) -> dict[str, Scalar]:
    result: dict[str, Scalar] = {}
    for key, value in sorted(values.items()):
        if not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,64}", key) or _SENSITIVE.search(key):
            continue
        if isinstance(value, str):
            value = " ".join(value[:MAX_METADATA_VALUE_CHARS].split())
            if "://" in value or key.lower() in {"url", "website"}:
                try:
                    parsed = urlsplit(value)
                    if (
                        parsed.scheme not in {"http", "https"}
                        or not parsed.hostname
                        or parsed.port not in {None, 80, 443}
                    ):
                        continue
                    value = urlunsplit((parsed.scheme, parsed.hostname, parsed.path, "", ""))
                except ValueError:
                    continue
            if _SECRET_VALUE.search(value) or (
                "@" in value and re.search(r"[\w.+-]{1,128}@[\w.-]{1,128}\.[a-z]{2,}", value, re.I)
            ):
                continue
            value = value[:MAX_METADATA_VALUE_CHARS]
        proposed = result | {key: value}
        if len(json.dumps(proposed, ensure_ascii=False).encode()) > MAX_METADATA_BYTES:
            continue
        result = proposed
        if len(result) == MAX_METADATA_FIELDS:
            break
    return result
