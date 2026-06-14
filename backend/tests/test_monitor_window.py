"""Tests for the date-range windowing + new shapes in build_monitor_overview()."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, Chat, Message, ToolCall, Usage


def _seed_and_build(tmp_path):
    async def run():
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'monitor.db'}")
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        Session = async_sessionmaker(engine, expire_on_commit=False)
        now = datetime.now(timezone.utc)
        async with Session() as s:
            s.add(Chat(id="c1", tenant_id="t1", user_id="u1", title="Chat"))
            # Messages at: now, 5 days ago, 20 days ago.
            for i, age in enumerate((0, 5, 20)):
                s.add(Message(id=f"m{i}", chat_id="c1", role="user", content="hi", created_at=now - timedelta(days=age)))
            # One assistant message (provider mix) at now.
            s.add(Message(id="ma", chat_id="c1", role="assistant", content="ok", provider="openai", created_at=now))
            # Tool calls at: now, 5 days ago, 20 days ago.
            for i, age in enumerate((0, 5, 20)):
                s.add(ToolCall(id=f"tc{i}", tenant_id="t1", chat_id="c1", tool_name="foo", kind="read", status="succeeded", duration_ms=100, created_at=now - timedelta(days=age)))
            # Usage at now + 20 days ago.
            s.add(Usage(id="u0", tenant_id="t1", user_id="u1", chat_id="c1", model="gpt", prompt_tokens=100, completion_tokens=50, created_at=now))
            s.add(Usage(id="u1", tenant_id="t1", user_id="u1", chat_id="c1", model="gpt", prompt_tokens=999, completion_tokens=999, created_at=now - timedelta(days=20)))
            await s.commit()

        from app.api.admin import build_monitor_overview

        async with Session() as s:
            life = await build_monitor_overview(s, "t1", days=None)
            d7 = await build_monitor_overview(s, "t1", days=7)
            d1 = await build_monitor_overview(s, "t1", days=1)
        await engine.dispose()
        return life, d7, d1

    return asyncio.run(run())


def test_windowing_scopes_activity_counts(tmp_path):
    life, d7, d1 = _seed_and_build(tmp_path)
    # 3 user messages lifetime; 7d window keeps now+5d; 1d window keeps only now.
    # (the assistant message at `now` is also counted as a message)
    assert life["totals"]["messages"] == 4
    assert d7["totals"]["messages"] == 3  # now(2) + 5d(1)
    assert d1["totals"]["messages"] == 2  # now only (user + assistant)
    # Tool calls: 3 lifetime, 2 in 7d, 1 in 1d.
    assert life["totals"]["tool_calls"] == 3
    assert d7["totals"]["tool_calls"] == 2
    assert d1["totals"]["tool_calls"] == 1


def test_window_meta_and_tokens_windowed(tmp_path):
    life, d7, d1 = _seed_and_build(tmp_path)
    assert life["window"] == {"days": None, "since": None}
    assert d7["window"]["days"] == 7 and d7["window"]["since"]
    # Tokens: lifetime includes the 20-day-old big usage; 7d excludes it.
    assert life["tokens"]["total"] == 100 + 50 + 999 + 999
    assert d7["tokens"]["total"] == 150
    assert d1["tokens"]["total"] == 150


def test_activity_range_adaptive_buckets(tmp_path):
    life, d7, d1 = _seed_and_build(tmp_path)
    # Lifetime defaults to a 14-day daily series.
    assert len(life["activity_range"]) == 14 and life["activity_range"][0]["bucket"] == "day"
    # 7d → 7 daily points; 1d → 24 hourly points.
    assert len(d7["activity_range"]) == 7 and d7["activity_range"][-1]["bucket"] == "day"
    assert len(d1["activity_range"]) == 24 and d1["activity_range"][0]["bucket"] == "hour"
    # Each point has the three series keys.
    p = d7["activity_range"][0]
    assert {"ts", "bucket", "messages", "tool_calls", "runs"} <= set(p)


def test_heatmap_shape_is_7x24(tmp_path):
    life, d7, _ = _seed_and_build(tmp_path)
    for snap in (life, d7):
        hm = snap["heatmap"]
        assert len(hm["matrix"]) == 7
        assert all(len(row) == 24 for row in hm["matrix"])
        assert isinstance(hm["max"], int)
    # There is activity, so the lifetime heatmap max is > 0.
    assert life["heatmap"]["max"] > 0


def test_chats_windowed_to_active_in_period(tmp_path):
    life, d7, d1 = _seed_and_build(tmp_path)
    # Lifetime = total chats (1). Windowed = chats active in the period (still 1 here).
    assert life["totals"]["chats"] == 1
    assert d7["totals"]["chats"] == 1
    assert d1["totals"]["chats"] == 1
