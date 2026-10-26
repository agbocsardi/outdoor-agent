"""ClosureProvider adapter for the current and planned NDW feeds."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from outdoor_agent.config import Settings
from outdoor_agent.providers.base import ClosureFeedResult
from outdoor_agent.providers.ndw import FeedFetchResult, fetch_feeds
from outdoor_agent.providers.registry import Provider, registry


class NDWClosureProvider:
    declaration: Provider = next(item for item in registry() if item.provider == "ndw")

    def __init__(
        self,
        settings: Settings,
        *,
        fetcher: Callable[[Settings], Awaitable[FeedFetchResult]] = fetch_feeds,
    ) -> None:
        self.settings = settings
        self.fetcher = fetcher

    async def list_events(
        self,
        bbox: tuple[float, float, float, float],
        window: str,
        mode: str,
    ) -> ClosureFeedResult:
        # NDW feed records are route-independent; deterministic spatial/time/mode
        # filtering is performed by validation after both snapshots are retained.
        del bbox, window, mode
        return await self.fetcher(self.settings)
