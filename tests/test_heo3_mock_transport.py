"""Tests for the MockTransport.

Per `feedback_dry_run.md`: do NOT test the writer via dry_run — use
this mock instead. These tests pin the mock's contract so future
adapter tests can rely on it.
"""

from __future__ import annotations

import pytest

from heo3.transport import MockTransport, _topic_matches


class TestPublish:
    @pytest.mark.asyncio
    async def test_records_publishes(self):
        t = MockTransport()
        await t.connect()
        await t.publish("foo/bar", "value1")
        await t.publish("foo/baz", "value2")
        assert len(t.published) == 2
        assert t.published[0].topic == "foo/bar"
        assert t.published[0].payload == "value1"
        assert t.published[1].payload == "value2"

    @pytest.mark.asyncio
    async def test_publish_before_connect_raises(self):
        t = MockTransport()
        with pytest.raises(RuntimeError, match="before connect"):
            await t.publish("foo", "bar")

    @pytest.mark.asyncio
    async def test_clear(self):
        t = MockTransport()
        await t.connect()
        await t.publish("a", "b")
        t.clear()
        assert t.published == []


class TestSubscribeAndInject:
    @pytest.mark.asyncio
    async def test_inject_calls_handler(self):
        t = MockTransport()
        received: list[tuple[str, str]] = []

        async def handler(topic: str, payload: str) -> None:
            received.append((topic, payload))

        await t.subscribe("response/+", handler)
        await t.inject("response/state", "Saved")
        assert received == [("response/state", "Saved")]

    @pytest.mark.asyncio
    async def test_inject_to_unsubscribed_topic_is_silent(self):
        t = MockTransport()
        received: list[tuple[str, str]] = []

        async def handler(topic: str, payload: str) -> None:
            received.append((topic, payload))

        await t.subscribe("foo/+", handler)
        await t.inject("bar/state", "ignored")
        assert received == []

    @pytest.mark.asyncio
    async def test_multiple_handlers_per_topic(self):
        t = MockTransport()
        calls: list[str] = []

        async def h1(topic: str, payload: str) -> None:
            calls.append(f"h1:{payload}")

        async def h2(topic: str, payload: str) -> None:
            calls.append(f"h2:{payload}")

        await t.subscribe("x/y", h1)
        await t.subscribe("x/y", h2)
        await t.inject("x/y", "v")
        assert calls == ["h1:v", "h2:v"]


class TestConnectionState:
    @pytest.mark.asyncio
    async def test_default_disconnected(self):
        assert MockTransport().is_connected is False

    @pytest.mark.asyncio
    async def test_connect_disconnect_cycle(self):
        t = MockTransport()
        await t.connect()
        assert t.is_connected is True
        await t.disconnect()
        assert t.is_connected is False


class TestTopicMatcher:
    """Pin the wildcard semantics — tests for SA-style topics."""

    def test_exact_match(self):
        assert _topic_matches("solar_assistant/inverter_1/x", "solar_assistant/inverter_1/x")

    def test_no_match_different_levels(self):
        assert not _topic_matches("solar_assistant/inverter_1/x", "solar_assistant/inverter_1/y")

    def test_plus_wildcard_single_level(self):
        assert _topic_matches("solar_assistant/+/state", "solar_assistant/inverter_1/state")
        assert not _topic_matches("solar_assistant/+/state", "solar_assistant/inverter_1/sub/state")

    def test_hash_wildcard_multi_level(self):
        assert _topic_matches("solar_assistant/#", "solar_assistant/inverter_1/state")
        assert _topic_matches("solar_assistant/#", "solar_assistant/x/y/z")

    def test_response_topic_pattern(self):
        # The exact pattern HEO III subscribes to per §16.
        assert _topic_matches(
            "solar_assistant/set/response_message/state",
            "solar_assistant/set/response_message/state",
        )
