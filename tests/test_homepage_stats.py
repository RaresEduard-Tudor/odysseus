"""Regression test for routes.homepage_routes._gather (the customapi widget feed).

Uses a real in-memory SQLite seeded for two owners so it pins:
  - owner-scoping (alice's numbers exclude bob's rows),
  - that the count/next-event/task sections survive a real schema, and
  - fuel parsing from the action's text output.

If a model's columns drift in a way that breaks the queries, this fails loudly
instead of the homepage tile silently showing nulls.
"""
import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import core.database as db
from core.database import (
    Base, ScheduledTask, Memory, Document, Session as DbSession,
    ChatMessage, CalendarCal, CalendarEvent,
)
import routes.homepage_routes as hp


@pytest.fixture
def seeded_sessionlocal(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Local = sessionmaker(bind=engine)

    now = datetime.utcnow()
    s = Local()
    # alice: 2 active tasks (+1 paused), 3 memories, 2 docs, 1 session w/ 4 msgs,
    # 1 calendar with a future event.
    s.add_all([
        ScheduledTask(id="t1", owner="alice", name="Fuel Price", status="active",
                      next_run=now + timedelta(hours=2)),
        ScheduledTask(id="t2", owner="alice", name="Email Summary", status="active",
                      next_run=now + timedelta(hours=5)),
        ScheduledTask(id="t3", owner="alice", name="Paused One", status="paused",
                      next_run=now + timedelta(hours=1)),
        ScheduledTask(id="t9", owner="bob", name="Bob Task", status="active",
                      next_run=now + timedelta(minutes=10)),
        Memory(id="m1", owner="alice", text="a"),
        Memory(id="m2", owner="alice", text="b"),
        Memory(id="m3", owner="alice", text="c"),
        Memory(id="m9", owner="bob", text="z"),
        Document(id="d1", owner="alice", title="Doc One"),
        Document(id="d2", owner="alice", title="Doc Two"),
        Document(id="d9", owner="bob", title="Bob Doc"),
        DbSession(id="sess1", owner="alice", name="chat", endpoint_url="", model=""),
        DbSession(id="sess9", owner="bob", name="chat", endpoint_url="", model=""),
        CalendarCal(id="cal1", owner="alice", name="Personal"),
        CalendarCal(id="cal9", owner="bob", name="Bob Cal"),
    ])
    s.commit()
    for i in range(4):
        s.add(ChatMessage(id=f"msg{i}", session_id="sess1", role="user", content="hi"))
    s.add(ChatMessage(id="msgbob", session_id="sess9", role="user", content="hi"))
    s.add(CalendarEvent(uid="ev1", calendar_id="cal1", summary="Dentist",
                        dtstart=now + timedelta(hours=3), dtend=now + timedelta(hours=4)))
    s.add(CalendarEvent(uid="ev9", calendar_id="cal9", summary="Bob Meeting",
                        dtstart=now + timedelta(hours=1), dtend=now + timedelta(hours=2)))
    s.commit()
    s.close()

    monkeypatch.setattr(db, "SessionLocal", Local)

    # Keep the test offline: stub the live fuel scrape and IMAP.
    async def fake_fuel(owner, fuel=""):
        return ("Diesel B7: no change (2,0800 €/l)\n"
                "Super 95 E10: no change (1,8740 €/l)", True)
    import src.builtin_actions as ba
    monkeypatch.setattr(ba, "action_get_fuel_price", fake_fuel)
    import routes.email_helpers as eh
    monkeypatch.setattr(eh, "_imap", lambda *a, **k: None)
    return Local


def test_gather_owner_scoped_counts(seeded_sessionlocal):
    out = hp._gather("alice")
    # counts are alice-only (bob's rows excluded)
    assert out["tasks_active"] == 2          # not 3 (paused) and not bob's
    assert out["memories"] == 3
    assert out["documents"] == 2
    assert out["messages_week"] == 4         # bob's msg excluded
    # next task = soonest active alice task
    assert out["next_task"].startswith("Fuel Price")
    # next event = alice's future event, not bob's
    assert out["next_event"].startswith("Dentist")
    # fuel parsed from the action text
    assert out["fuel_diesel"] == "2,0800"
    assert out["fuel_95"] == "1,8740"
    # combined one-field view
    assert out["fuel"] == "Diesel 2,0800 · 95 1,8740"
    # briefing assembled
    assert "Diesel 2,0800" in out["briefing"]


def test_gather_excludes_other_owner(seeded_sessionlocal):
    bob = hp._gather("bob")
    assert bob["tasks_active"] == 1
    assert bob["memories"] == 1
    assert bob["next_event"].startswith("Bob Meeting")
