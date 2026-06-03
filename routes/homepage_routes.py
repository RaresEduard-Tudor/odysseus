"""Homepage dashboard stats — /api/homepage-stats.

A single JSON endpoint for gethomepage.dev's `customapi` widget. Aggregates
cheap, glanceable numbers (fuel prices, next calendar event, scheduled tasks,
RAG/memory sizes, activity, a one-line briefing) for the Odysseus tile.

Each section is computed in its own try/except so a slow or failing source
(e.g. live fuel scrape) returns null for that field instead of 500-ing the
whole widget. The full payload is cached per-owner for CACHE_TTL seconds so
homepage's frequent polling does not hammer the DB or re-scrape carbu.com.
"""

import logging
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from fastapi import APIRouter, Request

from src.auth_helpers import effective_user

logger = logging.getLogger(__name__)

CACHE_TTL = 60  # seconds
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


def _fmt_when(dt: datetime) -> str:
    """Short, human relative-ish label for an upcoming datetime."""
    now = datetime.utcnow()
    delta = dt - now
    if delta.total_seconds() < 0:
        return dt.strftime("%a %H:%M")
    if delta < timedelta(hours=1):
        return f"in {int(delta.total_seconds() // 60)}m"
    if delta < timedelta(hours=24) and dt.date() == now.date():
        return f"today {dt.strftime('%H:%M')}"
    if delta < timedelta(days=2):
        return f"tomorrow {dt.strftime('%H:%M')}"
    return dt.strftime("%a %H:%M")


def _gather(owner: str) -> Dict[str, Any]:
    from core.database import (
        SessionLocal, ScheduledTask, Memory, Document, ChatMessage,
        Session as DbSession, CalendarEvent, CalendarCal, EmailAccount,
    )

    out: Dict[str, Any] = {}
    now = datetime.utcnow()

    def scoped(query, model):
        # Single-user / anonymous (owner == "") sees everything; otherwise
        # restrict to the caller's rows.
        return query.filter(model.owner == owner) if owner else query

    db = SessionLocal()
    try:
        # ── Fuel prices (live scrape, cached by this payload) ──
        try:
            import asyncio
            from src.builtin_actions import action_get_fuel_price
            msg, ok = asyncio.run(action_get_fuel_price(owner or "homepage"))
            if ok:
                # lines like "Diesel B7: no change (2,0800 €/l)" or
                # "Diesel B7: 2,08 → 2,10 ↑ €/l"
                import re
                for line in msg.splitlines():
                    label = line.split(":", 1)[0].strip().lower()
                    price = re.search(r"(\d,\d{2,4})", line)
                    if not price:
                        continue
                    val = price.group(1)
                    if "diesel" in label and "b7" in label:
                        out["fuel_diesel"] = val
                    elif "95" in label:
                        out["fuel_95"] = val
                    elif "98" in label and "e5" in label:
                        out["fuel_98"] = val
                # Combined one-field view: "Diesel 2,08 · 95 1,87 · 98 1,95".
                combined = []
                if out.get("fuel_diesel"):
                    combined.append(f"Diesel {out['fuel_diesel']}")
                if out.get("fuel_95"):
                    combined.append(f"95 {out['fuel_95']}")
                if out.get("fuel_98"):
                    combined.append(f"98 {out['fuel_98']}")
                if combined:
                    out["fuel"] = " · ".join(combined)
        except Exception as e:
            logger.debug("homepage fuel section failed: %s", e)

        # ── Calendar: next event + count today ──
        try:
            q = (
                db.query(CalendarEvent)
                .join(CalendarCal, CalendarEvent.calendar_id == CalendarCal.id)
                .filter(CalendarEvent.status != "cancelled")
            )
            if owner:
                q = q.filter(CalendarCal.owner == owner)
            upcoming = (
                q.filter(CalendarEvent.dtstart >= now)
                .order_by(CalendarEvent.dtstart.asc())
                .first()
            )
            if upcoming:
                out["next_event"] = f"{upcoming.summary} · {_fmt_when(upcoming.dtstart)}"
            end_of_day = now.replace(hour=23, minute=59, second=59)
            out["events_today"] = (
                q.filter(CalendarEvent.dtstart >= now.replace(hour=0, minute=0, second=0))
                .filter(CalendarEvent.dtstart <= end_of_day)
                .count()
            )
        except Exception as e:
            logger.debug("homepage calendar section failed: %s", e)

        # ── Scheduled tasks: active count + next due ──
        try:
            active = scoped(
                db.query(ScheduledTask).filter(ScheduledTask.status == "active"),
                ScheduledTask,
            )
            out["tasks_active"] = active.count()
            nxt = (
                active.filter(ScheduledTask.next_run.isnot(None))
                .order_by(ScheduledTask.next_run.asc())
                .first()
            )
            if nxt and nxt.next_run:
                out["next_task"] = f"{nxt.name} · {_fmt_when(nxt.next_run)}"
        except Exception as e:
            logger.debug("homepage tasks section failed: %s", e)

        # ── Knowledge base sizes ──
        try:
            out["memories"] = scoped(db.query(Memory), Memory).count()
        except Exception as e:
            logger.debug("homepage memories failed: %s", e)
        try:
            out["documents"] = scoped(db.query(Document), Document).count()
            latest = (
                scoped(db.query(Document), Document)
                .order_by(Document.updated_at.desc())
                .first()
            )
            if latest and getattr(latest, "title", None):
                out["latest_doc"] = latest.title
        except Exception as e:
            logger.debug("homepage documents failed: %s", e)

        # ── Activity: chats today + messages this week ──
        try:
            sess_ids = [s.id for s in scoped(db.query(DbSession), DbSession).all()]
            if sess_ids:
                day_ago = now - timedelta(days=1)
                week_ago = now - timedelta(days=7)
                out["chats_today"] = (
                    db.query(DbSession)
                    .filter(DbSession.id.in_(sess_ids))
                    .filter(DbSession.updated_at >= day_ago)
                    .count()
                )
                out["messages_week"] = (
                    db.query(ChatMessage)
                    .filter(ChatMessage.session_id.in_(sess_ids))
                    .filter(ChatMessage.timestamp >= week_ago)
                    .count()
                )
        except Exception as e:
            logger.debug("homepage activity failed: %s", e)

        # ── Email (best-effort, may be null if not configured/slow) ──
        # Resolve an account first: the caller's, or — when anonymous (owner
        # "", e.g. the auth-exempt homepage widget) — the first enabled one,
        # default preferred. _imap is a context manager, not a connection.
        try:
            from routes.email_helpers import _imap
            acc_q = db.query(EmailAccount).filter(EmailAccount.enabled == True)  # noqa: E712
            if owner:
                acc_q = acc_q.filter(EmailAccount.owner == owner)
            acc = acc_q.order_by(
                EmailAccount.is_default.desc(), EmailAccount.created_at.asc()
            ).first()
            if acc:
                with _imap(account_id=acc.id, owner=acc.owner or "") as conn:
                    conn.select("INBOX", readonly=True)
                    typ, data = conn.search(None, "UNSEEN")
                    if typ == "OK":
                        out["unread_email"] = len(data[0].split()) if data and data[0] else 0
                    typ2, data2 = conn.search(None, '(KEYWORD "urgent")')
                    if typ2 == "OK":
                        out["urgent_email"] = len(data2[0].split()) if data2 and data2[0] else 0
        except Exception as e:
            logger.debug("homepage email section failed: %s", e)

        # ── One-line briefing assembled from the above ──
        try:
            bits = []
            if out.get("fuel_diesel"):
                bits.append(f"Diesel {out['fuel_diesel']}")
            if out.get("next_event"):
                bits.append(out["next_event"])
            if out.get("urgent_email"):
                bits.append(f"{out['urgent_email']} urgent mail")
            elif out.get("unread_email") is not None:
                bits.append(f"{out['unread_email']} unread")
            if out.get("next_task"):
                bits.append(f"⏰ {out['next_task']}")
            if bits:
                out["briefing"] = " · ".join(bits)
        except Exception as e:
            logger.debug("homepage briefing failed: %s", e)
    finally:
        db.close()

    return out


def setup_homepage_routes() -> APIRouter:
    router = APIRouter(tags=["homepage"])

    @router.get("/api/homepage-stats")
    async def homepage_stats(request: Request) -> Dict[str, Any]:
        owner = effective_user(request) or ""
        cached = _cache.get(owner)
        if cached and (time.time() - cached[0]) < CACHE_TTL:
            return cached[1]
        # Run in a worker thread: _gather does blocking DB/IMAP work and uses
        # asyncio.run() for the fuel scrape, which is illegal on the running loop.
        import asyncio
        data = await asyncio.to_thread(_gather, owner)
        _cache[owner] = (time.time(), data)
        return data

    return router
