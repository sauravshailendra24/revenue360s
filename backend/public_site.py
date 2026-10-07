"""Public site, contact capture, and landing analytics.

Drop next to the other routers. Does not change auth or the workspace modules.
Wire routes in main.py as described in README.md.
"""
from __future__ import annotations

import time
from fastapi import APIRouter, Depends, HTTPException, Request

from auth import user_from_header
from db import env, execute, new_id, normalize_email, now_iso, one, query

router = APIRouter(tags=["public-site"])
_HITS: dict[str, list[float]] = {}


def ensure_public_tables() -> None:
    execute(
        """CREATE TABLE IF NOT EXISTS landing_sessions (
            id VARCHAR(40) PRIMARY KEY,
            session_id VARCHAR(80),
            visitor_id VARCHAR(80),
            user_id VARCHAR(40) DEFAULT '',
            landing_version VARCHAR(32) DEFAULT '',
            variant VARCHAR(8) DEFAULT 'A',
            utm_source VARCHAR(120) DEFAULT '',
            utm_medium VARCHAR(120) DEFAULT '',
            utm_campaign VARCHAR(160) DEFAULT '',
            utm_content VARCHAR(160) DEFAULT '',
            utm_term VARCHAR(160) DEFAULT '',
            referrer VARCHAR(300) DEFAULT '',
            landing_path VARCHAR(180) DEFAULT '',
            device_type VARCHAR(20) DEFAULT '',
            country VARCHAR(8) DEFAULT '',
            first_seen_at VARCHAR(40),
            last_seen_at VARCHAR(40)
        )"""
    )
    execute(
        """CREATE TABLE IF NOT EXISTS landing_events (
            id VARCHAR(40) PRIMARY KEY,
            session_id VARCHAR(80),
            visitor_id VARCHAR(80),
            user_id VARCHAR(40) DEFAULT '',
            event_name VARCHAR(80),
            event_data TEXT,
            page VARCHAR(180) DEFAULT '',
            section VARCHAR(80) DEFAULT '',
            landing_version VARCHAR(32) DEFAULT '',
            variant VARCHAR(8) DEFAULT 'A',
            utm_source VARCHAR(120) DEFAULT '',
            created_at VARCHAR(40)
        )"""
    )
    execute(
        """CREATE TABLE IF NOT EXISTS contact_leads (
            id VARCHAR(40) PRIMARY KEY,
            name VARCHAR(160),
            company VARCHAR(160) DEFAULT '',
            email VARCHAR(180),
            phone VARCHAR(40) DEFAULT '',
            message TEXT,
            source VARCHAR(80) DEFAULT '',
            landing_version VARCHAR(32) DEFAULT '',
            created_at VARCHAR(40)
        )"""
    )


def _throttle(key: str, limit: int = 20, window: int = 600) -> None:
    now = time.time()
    recent = [stamp for stamp in _HITS.get(key, []) if now - stamp < window]
    if len(recent) >= limit:
        raise HTTPException(429, "Too many requests. Try again shortly.")
    recent.append(now)
    _HITS[key] = recent


def _admin(user: dict = Depends(user_from_header)) -> dict:
    allowed = (env("R360_ADMIN_EMAIL") or "").strip().lower()
    if not allowed or user.get("email", "").lower() != allowed:
        raise HTTPException(403, "Admin only.")
    return user


@router.post("/api/public/events")
async def track(request: Request) -> dict:
    payload = await request.json()
    session_id = str(payload.get("session_id") or "")[:80]
    visitor_id = str(payload.get("visitor_id") or "")[:80]
    if not session_id or not visitor_id:
        raise HTTPException(400, "session_id and visitor_id are required.")
    _throttle("event:" + visitor_id, limit=120)
    existing = one("SELECT id FROM landing_sessions WHERE session_id = ?", (session_id,))
    now = now_iso()
    if existing:
        execute("UPDATE landing_sessions SET last_seen_at = ? WHERE id = ?", (now, existing["id"]))
    else:
        execute(
            """INSERT INTO landing_sessions
            (id, session_id, visitor_id, landing_version, variant, utm_source, utm_medium, utm_campaign, utm_content, utm_term, referrer, landing_path, device_type, first_seen_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id(), session_id, visitor_id,
                str(payload.get("landing_version") or "")[:32],
                str(payload.get("variant") or "A")[:8],
                str(payload.get("utm_source") or "")[:120],
                str(payload.get("utm_medium") or "")[:120],
                str(payload.get("utm_campaign") or "")[:160],
                str(payload.get("utm_content") or "")[:160],
                str(payload.get("utm_term") or "")[:160],
                str(payload.get("referrer") or "")[:300],
                str(payload.get("path") or "")[:180],
                str(payload.get("device_type") or "")[:20],
                now, now,
            ),
        )
    import json
    execute(
        """INSERT INTO landing_events
        (id, session_id, visitor_id, event_name, event_data, page, section, landing_version, variant, utm_source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id(), session_id, visitor_id,
            str(payload.get("event_name") or "event")[:80],
            json.dumps(payload.get("event_data") or {})[:4000],
            str(payload.get("page") or "")[:180],
            str(payload.get("section") or "")[:80],
            str(payload.get("landing_version") or "")[:32],
            str(payload.get("variant") or "A")[:8],
            str(payload.get("utm_source") or "")[:120],
            now,
        ),
    )
    return {"ok": True}


@router.post("/api/public/contact")
async def contact(request: Request) -> dict:
    payload = await request.json()
    email = normalize_email(payload.get("email", ""))
    name = str(payload.get("name") or "").strip()
    message = str(payload.get("message") or "").strip()
    if not name or not email or "@" not in email or len(message) < 4:
        raise HTTPException(400, "Name, email, and a short message are required.")
    _throttle("contact:" + email, limit=5)
    execute(
        """INSERT INTO contact_leads (id, name, company, email, phone, message, source, landing_version, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            new_id(), name[:160], str(payload.get("company") or "")[:160], email,
            str(payload.get("phone") or "")[:40], message[:4000],
            str(payload.get("source") or "")[:80], str(payload.get("landing_version") or "")[:32], now_iso(),
        ),
    )
    sales = env("R360_SALES_EMAIL", "sales@stock360s.com")
    try:
        from engine import send_email
        send_email("", sales, "Revenue360s contact", f"{name} <{email}>\n{payload.get('phone') or ''}\n\n{message}\n")
    except Exception:
        pass
    return {"ok": True}


@router.post("/api/public/attach")
def attach(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    visitor_id = str(payload.get("visitor_id") or "")[:80]
    if not visitor_id:
        raise HTTPException(400, "visitor_id is required.")
    execute(
        "UPDATE landing_sessions SET user_id = ? WHERE visitor_id = ? AND (user_id = '' OR user_id IS NULL)",
        (user["user_id"], visitor_id),
    )
    return {"ok": True}


@router.get("/api/public/plans")
def public_plans() -> dict:
    try:
        rows = query(
            """SELECT name, service_code, tier, mode, price, price_version, sellable
               FROM plans WHERE sellable = 1 ORDER BY price ASC"""
        )
    except Exception:
        return {"plans": [], "source": "unconfigured"}
    return {"plans": rows or [], "source": "catalog"}


@router.get("/api/admin/marketing/summary")
def marketing_summary(user: dict = Depends(_admin)) -> dict:
    del user
    visitors = one("SELECT COUNT(DISTINCT visitor_id) AS n FROM landing_sessions") or {"n": 0}
    signups = one("SELECT COUNT(*) AS n FROM landing_events WHERE event_name = 'signup_complete'") or {"n": 0}
    contacts = one("SELECT COUNT(*) AS n FROM contact_leads") or {"n": 0}
    whatsapp = one("SELECT COUNT(*) AS n FROM landing_events WHERE event_name = 'whatsapp_click'") or {"n": 0}
    demos = one("SELECT COUNT(*) AS n FROM landing_events WHERE event_name = 'demo_view'") or {"n": 0}
    return {
        "visitors": visitors["n"],
        "signups": signups["n"],
        "contacts": contacts["n"],
        "whatsapp_clicks": whatsapp["n"],
        "demo_views": demos["n"],
    }


@router.get("/api/admin/marketing/funnel")
def marketing_funnel(user: dict = Depends(_admin)) -> dict:
    del user
    names = ["landing_view", "journey_start", "product_section_view", "pricing_view", "signup_click", "signup_complete"]
    steps = []
    for name in names:
        row = one("SELECT COUNT(*) AS n FROM landing_events WHERE event_name = ?", (name,)) or {"n": 0}
        steps.append({"event": name, "count": row["n"]})
    return {"steps": steps}


@router.get("/api/admin/marketing/traffic")
def marketing_traffic(user: dict = Depends(_admin)) -> dict:
    del user
    rows = query(
        """SELECT COALESCE(NULLIF(utm_source, ''), 'direct') AS source, COUNT(*) AS visitors
           FROM landing_sessions GROUP BY source ORDER BY visitors DESC LIMIT 20"""
    )
    return {"sources": rows or []}


@router.get("/api/admin/marketing/events")
def marketing_events(user: dict = Depends(_admin)) -> dict:
    del user
    rows = query(
        """SELECT created_at, visitor_id, event_name, page, event_data, utm_source, landing_version
           FROM landing_events ORDER BY created_at DESC LIMIT 100"""
    )
    return {"events": rows or []}
