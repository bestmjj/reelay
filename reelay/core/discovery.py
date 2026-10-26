"""Bounded Telegram discovery for authorized, configured subscription sources.

Search only channels already configured for the user's own Telethon session.
Results enter the *same* Pipeline.accept dedup path as live messages; no cloud
copy, file mutation, subscription change or privileged Telegram API is added.
"""
from __future__ import annotations

import re
import unicodedata

NAMESPACE = "subscription_discovery"
INTERVAL_SEC = 1800           # each subscribed show/source at most twice an hour
SEARCH_LIMIT = 12             # Telegram search returns newest first
MAX_QUERIES_PER_CYCLE = 30    # bounds Telegram calls on large subscription lists
MAX_AGE_DAYS = 7              # restart history upper bound for an existing scanner
INITIAL_LOOKBACK_HOURS = 24   # first enablement must not bulk-copy a 7-day archive
MAX_CANDIDATES_PER_SHOW = 4   # avoid flooding the task queue on first scan
QUERY_DELAY_SEC = 2.0         # polite throttle; FloodWait is handled separately


def state_key(source: str, subscription: dict) -> str:
    return source.casefold() + ":" + str(subscription["id"])


def contains_title(text: str, title: str) -> bool:
    """Search hints are untrusted: require the title in the actual message."""
    def normal(value: str) -> str:
        value = unicodedata.normalize("NFKC", value).casefold()
        return re.sub(r"[\s·•._\-《》【】\[\]（）()]+", "", value)
    wanted = normal(title)
    return len(wanted) >= 2 and wanted in normal(text)


def due(state: dict, now: float) -> bool:
    return now >= max(float(state.get("checked_at") or 0) + INTERVAL_SEC,
                      float(state.get("next_at") or 0))


def cutoff(state: dict, now: float) -> float:
    """Only recent messages become auto-processing candidates.

    Subsequent runs overlap one hour for eventual-consistency and catch-up;
    the message dedup ledger makes overlap idempotent. For the very first
    run, restrict backlog to one day to avoid copying an old whole season.
    """
    previous = float(state.get("checked_at") or 0)
    earliest = previous - 3600 if previous else now - INITIAL_LOOKBACK_HOURS * 3600
    return max(now - MAX_AGE_DAYS * 86400, earliest)


def safe_status(state: dict) -> dict:
    """Public UI gets counts/timestamps, never raw Telegram message contents."""
    return {key: state.get(key) for key in ("source", "checked_at", "matches", "emitted", "error", "next_at")
            if key in state}
