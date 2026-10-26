"""Authorized Telegram subscription discovery; no live Telegram or cloud calls."""
from __future__ import annotations

import datetime as dt
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from reelay.core import discovery
from reelay.core.contracts import PipelineContext
from reelay.core.store import Store
from reelay.providers.source import TelegramUserbotSource


def msg(n: int, title: str, *, hours: float = 0, link: bool = True):
    stamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
    text = title + " 第106集"
    if link:
        text += " https://www.alipan.com/s/abc123"
    return SimpleNamespace(id=n, raw_text=text, entities=[], date=stamp, action=None)


class FakeSearchClient:
    def __init__(self, messages, *, error=None):
        self.messages, self.error, self.calls = messages, error, []

    def is_connected(self):
        return True

    async def get_entity(self, target):
        return SimpleNamespace(username="shareAliyun", id=99)

    async def iter_messages(self, entity, *, search, limit):
        self.calls.append((search, limit))
        if self.error:
            raise self.error
        for message in self.messages[:limit]:
            yield message


class FloodWait(Exception):
    seconds = 900


class SubscriptionDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.source = TelegramUserbotSource()
        self.source.configure(["@shareAliyun"])
        self.source._authorized = True
        self.values = {}
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.accepted = []

    def load(self, plugin, key, default):
        return self.values.get((plugin, key), default)

    def save(self, plugin, key, value):
        self.values[(plugin, key)] = value

    async def emit(self, raw):
        if self.store.accept_task(PipelineContext(raw=raw)):
            self.accepted.append(raw)

    async def test_finds_new_posts_from_one_configured_source_with_real_message_dedup(self):
        self.source._client = FakeSearchClient([
            msg(5, "牧神记", hours=1),
            msg(4, "牧神记", hours=3, link=False),
            msg(3, "牧神记", hours=48),
        ])
        active = [{"id": 12, "title": "牧神记", "season": 1, "auto": True, "finished": None},
                  {"id": 13, "title": "已完结", "auto": True, "finished": {"date": "2026-10-25"}},
                  {"id": 14, "title": "关闭自动", "auto": False}]
        with patch.object(discovery, "QUERY_DELAY_SEC", 0):
            result = await self.source.discover_subscriptions(self.emit, active, self.load, self.save)
            again = await self.source.discover_subscriptions(self.emit, active, self.load, self.save)
        self.assertEqual((result["queries"], result["candidates"], again["queries"]), (1, 1, 0))
        self.assertEqual([raw.msg_id for raw in self.accepted], ["5"])
        self.assertEqual(self.accepted[0].source, "@shareAliyun")
        self.assertTrue(self.accepted[0].extra["discovery"])
        key = discovery.state_key("@shareAliyun", active[0])
        self.assertGreater(self.values[(discovery.NAMESPACE, key)]["checked_at"], 0)
        self.assertEqual(len(self.source._client.calls), 1)
        # Even a forced repeat search can only create a single task.
        self.values[(discovery.NAMESPACE, key)]["checked_at"] = 1
        with patch.object(discovery, "QUERY_DELAY_SEC", 0):
            await self.source.discover_subscriptions(self.emit, active[:1], self.load, self.save)
        self.assertEqual(len(self.accepted), 1)

    async def test_flood_wait_keeps_retry_cursor_and_does_not_trigger_copy(self):
        self.source._client = FakeSearchClient([], error=FloodWait())
        active = [{"id": 1, "title": "牧神记", "auto": True, "finished": None}]
        with patch.object(discovery, "QUERY_DELAY_SEC", 0):
            result = await self.source.discover_subscriptions(self.emit, active, self.load, self.save)
        self.assertEqual(result["status"], "rate_limited")
        state = self.values[(discovery.NAMESPACE, discovery.state_key("@shareAliyun", active[0]))]
        self.assertGreater(state["next_at"], dt.datetime.now().timestamp() + 850)
        self.assertNotIn("checked_at", state)
        self.assertEqual(self.accepted, [])

    def test_normalized_title_and_safe_time_window(self):
        self.assertTrue(discovery.contains_title("《牧 神 记》 第 106 集", "牧神记"))
        self.assertFalse(discovery.contains_title("《仙逆》 第 1 集", "牧神记"))
        self.assertFalse(discovery.contains_title("a", "a"))
        self.assertFalse(discovery.due({"checked_at": 1000}, 1001))
        self.assertTrue(discovery.due({"checked_at": 1000}, 1000 + discovery.INTERVAL_SEC))
        now = 1000000
        self.assertEqual(discovery.cutoff({}, now), now - discovery.INITIAL_LOOKBACK_HOURS * 3600)
        self.assertGreaterEqual(discovery.cutoff({"checked_at": now - 3600}, now), now - 7200)


if __name__ == "__main__":
    unittest.main()
