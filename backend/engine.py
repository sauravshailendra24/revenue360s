from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException
from auth import user_from_header
from db import env, execute, insert, json_text, new_id, now_iso, one, public_url, query, utc_today

router = APIRouter(tags=["overview"])

@router.get("/api/me")
def me(authorization: str | None = None, user: dict = Depends(user_from_header)) -> dict:
    return user

@router.get("/api/overview")
def overview(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    pending = one(
        "SELECT COUNT(*) AS n FROM sequence_enrollments WHERE workspace_id = ? AND status = 'active' AND next_run_at <= ?",
        (ws, now_iso()),
    )
    from worker import public_jobs

    return {
        "leads": one("SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ?", (ws,))["n"],
        "messages": one("SELECT COUNT(*) AS n FROM messages WHERE workspace_id = ?", (ws,))["n"],
        "sequences": one("SELECT COUNT(*) AS n FROM sequences WHERE workspace_id = ? AND enabled = 1", (ws,))["n"],
        "campaigns": one("SELECT COUNT(*) AS n FROM campaigns WHERE workspace_id = ?", (ws,))["n"],
        "campaign_status": query("SELECT status, COUNT(*) AS count FROM campaigns WHERE workspace_id = ? GROUP BY status", (ws,)),
        "opens": one("SELECT COUNT(*) AS n FROM tracking_events WHERE workspace_id = ? AND event_type = 'open'", (ws,))["n"],
        "clicks": one("SELECT COUNT(*) AS n FROM tracking_events WHERE workspace_id = ? AND event_type = 'click'", (ws,))["n"],
        "emails_today": one(
            "SELECT COUNT(*) AS n FROM messages WHERE workspace_id = ? AND channel = 'email' AND direction = 'outbound' AND created_at LIKE ?",
            (ws, utc_today() + "%"),
        )["n"],
        "sms_today": one(
            "SELECT COUNT(*) AS n FROM messages WHERE workspace_id = ? AND channel = 'sms' AND direction = 'outbound' AND created_at LIKE ?",
            (ws, utc_today() + "%"),
        )["n"],
        "modes": {
            "email": delivery_mode(ws, "email"),
            "sms": delivery_mode(ws, "sms"),
            "whatsapp": delivery_mode(ws, "whatsapp"),
        },
        "due": {"pending": int((pending or {}).get("n") or 0), "executed": False},
        "refresh": public_jobs(ws),
        "activity": query(
            """
            SELECT created_at, 'lead' AS type, name AS detail FROM leads WHERE workspace_id = ?
            UNION ALL
            SELECT created_at, CONCAT(channel, ' ', direction), SUBSTRING(body,1,80) FROM messages WHERE workspace_id = ?
            ORDER BY created_at DESC LIMIT 15
            """,
            (ws, ws),
        ),
        "account_activity": query(
            """SELECT account_id, channel,
                      SUM(CASE WHEN direction = 'outbound' THEN 1 ELSE 0 END) AS sent,
                      SUM(CASE WHEN direction = 'inbound' THEN 1 ELSE 0 END) AS replies
               FROM messages WHERE workspace_id = ? AND account_id <> ''
               GROUP BY account_id, channel ORDER BY sent DESC""",
            (ws,),
        ),
        "webhook": f"{public_url()}/webhooks/meta",
        "verify_token": env("META_WEBHOOK_VERIFY_TOKEN", "revenue360s-verify"),
    }


@router.post("/api/worker/run")
def run_worker(user: dict = Depends(user_from_header)) -> dict:
    return process_due(user["workspace_id"])

overview_router = router
campaign_router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])
audience_router = APIRouter(prefix="/api/audiences", tags=["audiences"])


def _audience_conditions(workspace_id: str, filters: dict) -> tuple[list[str], list]:
    conditions = ["workspace_id = ?"]
    params = [workspace_id]
    for key, column in (("stage", "stage"), ("source", "source")):
        value = str(filters.get(key) or "").strip()
        if value:
            conditions.append(f"{column} = ?")
            params.append(value)
    tag = str(filters.get("tag") or "").strip()
    if tag:
        conditions.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if filters.get("has_email") is True:
        conditions.append("email <> ''")
    elif filters.get("has_email") is False:
        conditions.append("email = ''")
    if filters.get("has_phone") is True:
        conditions.append("phone <> ''")
    elif filters.get("has_phone") is False:
        conditions.append("phone = ''")
    if filters.get("has_whatsapp") is True:
        conditions.append("phone <> ''")
    elif filters.get("has_whatsapp") is False:
        conditions.append("phone = ''")
    for key, column, operator in (
        ("created_after", "created_at", ">="),
        ("created_before", "created_at", "<="),
        ("last_contacted_after", "last_contacted_at", ">="),
        ("last_contacted_before", "last_contacted_at", "<="),
    ):
        value = str(filters.get(key) or "").strip()
        if value:
            conditions.append(f"{column} {operator} ?")
            params.append(value)
    return conditions, params


def _audience_count(workspace_id: str, filters: dict) -> int:
    conditions, params = _audience_conditions(workspace_id, filters)
    return int(one(f"SELECT COUNT(*) AS n FROM leads WHERE {' AND '.join(conditions)}", tuple(params))["n"])


def _json_value(value: str | None, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


@audience_router.get("")
def list_audiences(user: dict = Depends(user_from_header)) -> list:
    workspace_id = user["workspace_id"]
    audiences = query("SELECT * FROM saved_audiences WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,))
    for audience in audiences:
        audience["filters"] = _json_value(audience.get("filters"), {})
        audience["lead_count"] = _audience_count(workspace_id, audience["filters"])
    return audiences


@audience_router.post("/preview")
def preview_audience(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    conditions, params = _audience_conditions(user["workspace_id"], filters)
    leads = query(
        f"SELECT id, name, email, phone, source, stage FROM leads WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT 100",
        tuple(params),
    )
    return {"count": _audience_count(user["workspace_id"], filters), "leads": leads}


@audience_router.post("")
def save_audience(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    name = str(payload.get("name") or "").strip()
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    if not name:
        raise HTTPException(400, "Audience name is required.")
    if len(name) > 255:
        raise HTTPException(400, "Audience name is too long.")
    audience_id = insert(
        "saved_audiences",
        {"name": name, "filters": json_text(filters), "updated_at": now_iso()},
        user["workspace_id"],
    )
    return {"id": audience_id, "lead_count": _audience_count(user["workspace_id"], filters)}


@audience_router.get("/{audience_id}/leads")
def audience_leads(audience_id: str, user: dict = Depends(user_from_header)) -> list:
    audience = one("SELECT * FROM saved_audiences WHERE id = ? AND workspace_id = ?", (audience_id, user["workspace_id"]))
    if not audience:
        raise HTTPException(404, "Audience not found.")
    conditions, params = _audience_conditions(user["workspace_id"], _json_value(audience.get("filters"), {}))
    return query(
        f"SELECT id, name, email, phone, source, stage FROM leads WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT 500",
        tuple(params),
    )


@campaign_router.get("")
def list_campaigns(user: dict = Depends(user_from_header)) -> list:
    campaigns = query("SELECT * FROM campaigns WHERE workspace_id = ? ORDER BY created_at DESC", (user["workspace_id"],))
    for campaign in campaigns:
        for key in ("audience_filters", "channels", "account_ids", "content", "limits", "tracking"):
            campaign[key] = _json_value(campaign.get(key), {} if key in {"audience_filters", "content", "limits", "tracking"} else [])
        filters = campaign["audience_filters"]
        campaign["lead_count"] = _audience_count(user["workspace_id"], filters)
    return campaigns


@campaign_router.post("")
def save_campaign(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    workspace_id = user["workspace_id"]
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Campaign name is required.")
    if len(name) > 255:
        raise HTTPException(400, "Campaign name is too long.")
    channels = payload.get("channels") if isinstance(payload.get("channels"), list) else []
    valid_channels = {"email", "sms", "whatsapp", "instagram", "facebook", "linkedin", "x", "tiktok", "youtube"}
    if any(channel not in valid_channels for channel in channels):
        raise HTTPException(400, "Campaign contains an unsupported channel.")
    account_ids = payload.get("account_ids") if isinstance(payload.get("account_ids"), list) else []
    account_tables = {
        "email_connections": ("email", "email"),
        "sms_connections": ("sms", "sms"),
        "whatsapp_connections": ("whatsapp", "whatsapp"),
        "social_accounts": ("social", "platform"),
        "meta_ad_accounts": ("ads", "meta_ads"),
    }
    found_accounts = {}
    for table, (account_type, platform_column) in account_tables.items():
        selected = "id, platform" if platform_column == "platform" else "id"
        table_rows = query(f"SELECT {selected} FROM {table} WHERE workspace_id = ?", (workspace_id,))
        found_accounts.update({row["id"]: row.get("platform") or account_type for row in table_rows})
    if any(account_id not in found_accounts for account_id in account_ids):
        raise HTTPException(400, "One or more selected accounts are unavailable in this workspace.")
    if any(found_accounts[account_id] not in channels for account_id in account_ids):
        raise HTTPException(400, "Selected accounts must match one of the campaign channels.")

    audience_id = str(payload.get("audience_id") or "")
    if audience_id:
        audience = one("SELECT * FROM saved_audiences WHERE id = ? AND workspace_id = ?", (audience_id, workspace_id))
        if not audience:
            raise HTTPException(404, "Audience not found.")
        audience_filters = _json_value(audience.get("filters"), {})
    else:
        audience_filters = payload.get("audience_filters") if isinstance(payload.get("audience_filters"), dict) else {}

    campaign_id = str(payload.get("id") or "").strip()
    existing = one("SELECT id FROM campaigns WHERE id = ? AND workspace_id = ?", (campaign_id, workspace_id)) if campaign_id else None
    values = {
        "name": name,
        "goal": str(payload.get("goal") or "")[:80],
        "status": payload.get("status") if payload.get("status") in {"draft", "scheduled"} else "draft",
        "audience_id": audience_id,
        "audience_filters": json_text(audience_filters),
        "channels": json_text(channels),
        "account_ids": json_text(account_ids),
        "content": json_text(payload.get("content") if isinstance(payload.get("content"), dict) else {}),
        "scheduled_at": str(payload.get("scheduled_at") or ""),
        "limits": json_text(payload.get("limits") if isinstance(payload.get("limits"), dict) else {}),
        "tracking": json_text(payload.get("tracking") if isinstance(payload.get("tracking"), dict) else {}),
        "updated_at": now_iso(),
    }
    if existing:
        assignments = ", ".join(f"{key} = ?" for key in values)
        execute(f"UPDATE campaigns SET {assignments} WHERE id = ? AND workspace_id = ?", (*values.values(), campaign_id, workspace_id))
    else:
        campaign_id = insert("campaigns", values, workspace_id)
    return {"id": campaign_id, "lead_count": _audience_count(workspace_id, audience_filters)}


@campaign_router.post("/{campaign_id}/status")
def set_campaign_status(campaign_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    status = str(payload.get("status") or "")
    if status not in {"draft", "scheduled", "running", "paused", "completed"}:
        raise HTTPException(400, "Unknown campaign status.")
    execute("UPDATE campaigns SET status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?", (status, now_iso(), campaign_id, user["workspace_id"]))
    return {"ok": True}


sequences_router = router

router = APIRouter(prefix="/api/sequences", tags=["sequences"])

router = APIRouter(prefix="/api/sequences", tags=["sequences"])


@router.get("")
def list_sequences(user: dict = Depends(user_from_header)) -> list:
    ws = user["workspace_id"]
    books = query("SELECT * FROM sequences WHERE workspace_id = ? ORDER BY created_at DESC", (ws,))
    for book in books:
        book["steps"] = query("SELECT * FROM sequence_steps WHERE sequence_id = ? ORDER BY step_order", (book["id"],))
        book["active"] = one("SELECT COUNT(*) AS n FROM sequence_enrollments WHERE sequence_id = ? AND status = 'active'", (book["id"],))["n"]
    return books


@router.post("")
def save_sequence(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    name = (payload.get("name") or "Lead warmup").strip()
    steps = payload.get("steps") or []
    if not steps:
        raise HTTPException(400, "Add at least one step.")
    for index, step in enumerate(steps, start=1):
        body = (step.get("body") or "").strip()
        if step.get("mode") != "ai" and not body:
            raise HTTPException(400, f"Write step {index} or switch it to AI.")
        channel = step.get("channel") or "whatsapp"
        account_id = str(step.get("account_id") or "").strip()
        account_table = {"email": "email_connections", "sms": "sms_connections", "whatsapp": "whatsapp_connections"}.get(channel)
        if account_id and (not account_table or not one(f"SELECT id FROM {account_table} WHERE id = ? AND workspace_id = ?", (account_id, ws))):
            raise HTTPException(400, f"Step {index} account is unavailable in this workspace.")
        variants = step.get("variants") or []
        if not isinstance(variants, list) or len(variants) > 4:
            raise HTTPException(400, f"Step {index} supports up to 4 alternative variants.")
        for variant in variants:
            if not isinstance(variant, dict) or not str(variant.get("body") or "").strip() or len(str(variant["body"])) > 4000:
                raise HTTPException(400, f"Step {index} has a variant with an empty or oversized body.")
    sequence_id = payload.get("id") or new_id()
    existing = one("SELECT id FROM sequences WHERE id = ? AND workspace_id = ?", (sequence_id, ws))
    values = {
        "name": name,
        "description": payload.get("description") or "",
        "enabled": int(payload.get("enabled", 1)),
        "stop_on_reply": int(payload.get("stop_on_reply", 1)),
        "trigger_whatsapp": int(payload.get("trigger_whatsapp", 1)),
        "trigger_sms": int(payload.get("trigger_sms", 0)),
        "trigger_email": int(payload.get("trigger_email", 0)),
        "trigger_new_lead": int(payload.get("trigger_new_lead", 1)),
    }
    if existing:
        execute(
            """UPDATE sequences SET name=?, description=?, enabled=?, stop_on_reply=?, trigger_whatsapp=?, trigger_sms=?, trigger_email=?, trigger_new_lead=?
               WHERE id=?""",
            (values["name"], values["description"], values["enabled"], values["stop_on_reply"], values["trigger_whatsapp"], values["trigger_sms"], values["trigger_email"], values["trigger_new_lead"], sequence_id),
        )
        execute("DELETE FROM sequence_steps WHERE sequence_id = ?", (sequence_id,))
    else:
        insert("sequences", {"id": sequence_id, **values}, ws)
    for index, step in enumerate(steps, start=1):
        body = (step.get("body") or "").strip()
        channel = step.get("channel") or "whatsapp"
        account_id = str(step.get("account_id") or "").strip()
        insert(
            "sequence_steps",
            {
                "sequence_id": sequence_id,
                "step_order": index,
                "delay_hours": float(step.get("delay_hours") or 0),
                "channel": channel,
                "account_id": account_id,
                "mode": step.get("mode") or "mine",
                "subject": step.get("subject") or "",
                "body": body,
                "variants": json_text([{"subject": str(v.get("subject") or ""), "body": str(v["body"]).strip()} for v in step["variants"]]) if step.get("variants") else "",
            },
            ws,
        )
    return {"id": sequence_id}


@router.post("/{sequence_id}/enroll")
def enroll_many(sequence_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    if not one("SELECT id FROM sequences WHERE id = ? AND workspace_id = ?", (sequence_id, ws)):
        raise HTTPException(404, "Sequence not found in this workspace.")
    count = 0
    for lead_id in payload.get("lead_ids") or []:
        if not one("SELECT id FROM leads WHERE id = ? AND workspace_id = ?", (lead_id, ws)):
            continue
        if enroll(ws, sequence_id, lead_id):
            count += 1
    return {"enrolled": count}


@router.get("/activity")
def activity(user: dict = Depends(user_from_header)) -> list:
    return query(
        """
        SELECT e.status, e.current_step, e.next_run_at, e.last_sent_at, s.name AS sequence_name,
               l.name AS lead_name, l.phone, l.email
        FROM sequence_enrollments e
        JOIN sequences s ON s.id = e.sequence_id
        JOIN leads l ON l.id = e.lead_id
        WHERE e.workspace_id = ?
        ORDER BY e.created_at DESC LIMIT 200
        """,
        (user["workspace_id"],),
    )


@router.post("/run")
def run_due(user: dict = Depends(user_from_header)) -> dict:
    return process_due(user["workspace_id"])


@router.get("/{sequence_id}/learning")
def sequence_learning(sequence_id: str, user: dict = Depends(user_from_header)) -> dict:
    """What the system has measured for each step: per-variant rate, sample size, and lift vs the baseline holdout."""
    ws = user["workspace_id"]
    if not one("SELECT id FROM sequences WHERE id = ? AND workspace_id = ?", (sequence_id, ws)):
        raise HTTPException(404, "Sequence not found in this workspace.")
    steps = []
    for step in query("SELECT * FROM sequence_steps WHERE sequence_id = ? AND workspace_id = ? ORDER BY step_order", (sequence_id, ws)):
        texts = {learning.variant_key(v["subject"], v["body"]): v for v in _step_variants(step)}
        report = learning.report(ws, f"seq:{sequence_id}:{step['step_order']}", list(texts))
        for arm in report["arms"]:
            variant = texts.get(arm["arm_key"])
            arm["subject"] = (variant or {}).get("subject", "")
            arm["body"] = (variant or {}).get("body", "(variant no longer in this step)")
        steps.append({"step": step["step_order"], "channel": step["channel"], **report})
    return {"steps": steps, "attribution_hours": learning.WINDOW_H}


sequences_router = router

router = APIRouter(prefix="/api/workflows", tags=["workflows"])


@router.get("")
def list_workflows(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    return {
        "rules": query(
            "SELECT * FROM automations WHERE workspace_id = ? ORDER BY created_at DESC",
            (ws,),
        ),
        "runs": query(
            "SELECT action_type, status, detail, created_at FROM automation_runs WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 30",
            (ws,),
        ),
        "sequences": query("SELECT id, name FROM sequences WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)),
    }


@router.post("")
def save_workflow(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    name = (payload.get("name") or "").strip()
    action_value = str(payload.get("action_value") or "").strip()
    if not name or not action_value:
        raise HTTPException(400, "Name and action value are required.")
    row_id = insert(
        "automations",
        {
            "name": name,
            "trigger_type": payload.get("trigger_type") or "new DM received",
            "keyword": payload.get("keyword") or "",
            "action_type": payload.get("action_type") or "enroll in sequence",
            "action_value": action_value,
            "enabled": int(payload.get("enabled", 1)),
        },
        user["workspace_id"],
    )
    return {"id": row_id}


workflows_router = router

import json
import re
import smtplib
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import quote

import requests

import learning
from db import (
    UPLOAD_DIR,
    decrypt_secret,
    encrypt_secret,
    env,
    execute,
    hours_from_now,
    insert,
    json_text,
    new_id,
    normalize_email,
    normalize_phone,
    now_iso,
    one,
    public_url,
    query,
    reveal,
    setting,
    utc_today,
)

UNSUB = {"stop", "unsubscribe", "cancel", "end", "quit"}
_UNSUB_STRONG = {"stop", "unsubscribe", "quit", "stopall"}


def is_unsubscribe(text: str) -> bool:
    """Whole-word match on short replies. "send" / "weekend" / "friend" must never count as STOP."""
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words or "don't" in words or "dont" in words:
        return False
    if len(words) == 1:
        return words[0] in UNSUB
    return len(words) <= 4 and (words[0] in _UNSUB_STRONG or words[-1] in _UNSUB_STRONG)


def _safe(fn, *args, default=None):
    """Learning must never break messaging."""
    try:
        return fn(*args)
    except Exception:
        return default
LINK_RE = re.compile(r"(https?://[^\s<>]+)", re.IGNORECASE)
PIXEL = (
    b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
    b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;"
)
SUBJECT_RE = re.compile(r"^\s*subject\s*:\s*(.+)$", re.IGNORECASE)


def company_context(workspace_id: str) -> str:
    profile = one(
        "SELECT * FROM company_profiles WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1",
        (workspace_id,),
    )
    docs = query(
        "SELECT filename, SUBSTRING(extracted_text,1,4000) AS extracted_text FROM company_documents WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 6",
        (workspace_id,),
    )
    parts = []
    if profile:
        parts.append(
            f"Company: {profile.get('company_name')}\nWebsite: {profile.get('website')}\n"
            f"Industry: {profile.get('industry')}\nOffer: {profile.get('offer')}\nVoice: {profile.get('brand_voice')}"
        )
    for doc in docs:
        if doc.get("extracted_text"):
            parts.append(f"Document {doc['filename']}:\n{doc['extracted_text']}")
    return "\n\n".join(parts)[:12000]


def find_lead(workspace_id: str, email: str = "", phone: str = "") -> dict | None:
    email_n = normalize_email(email)
    phone_n = normalize_phone(phone)
    if email_n:
        row = one(
            "SELECT * FROM leads WHERE workspace_id = ? AND email != '' AND LOWER(email) = ? ORDER BY created_at DESC LIMIT 1",
            (workspace_id, email_n),
        )
        if row:
            return row
    if phone_n:
        row = one(
            "SELECT * FROM leads WHERE workspace_id = ? AND phone_normalized = ? ORDER BY created_at DESC LIMIT 1",
            (workspace_id, phone_n),
        )
        if row:
            return row
    return None


def find_or_create_lead(workspace_id: str, name: str, phone: str, email: str, source: str = "manual", tags: list | None = None) -> tuple[dict, bool]:
    existing = find_lead(workspace_id, email, phone)
    if existing:
        updates, params = [], []
        if name and not existing.get("name"):
            updates.append("name = ?")
            params.append(name)
        if normalize_email(email) and not existing.get("email"):
            updates.append("email = ?")
            params.append(normalize_email(email))
        if phone and not existing.get("phone"):
            updates.append("phone = ?")
            params.append(phone.strip())
            updates.append("phone_normalized = ?")
            params.append(normalize_phone(phone))
        if updates:
            params.extend([existing["id"], workspace_id])
            execute(f"UPDATE leads SET {', '.join(updates)} WHERE id = ? AND workspace_id = ?", tuple(params))
            existing = one("SELECT * FROM leads WHERE id = ? AND workspace_id = ?", (existing["id"], workspace_id)) or existing
        return existing, False
    lead_id = insert(
        "leads",
        {
            "name": (name or "").strip(),
            "phone": (phone or "").strip(),
            "phone_normalized": normalize_phone(phone),
            "email": normalize_email(email),
            "source": source,
            "tags": json_text(tags or []),
            "stage": "new",
            "status": "new",
        },
        workspace_id,
    )
    return one("SELECT * FROM leads WHERE id = ?", (lead_id,)) or {}, True


def is_suppressed(workspace_id: str, address: str, channel: str) -> bool:
    addr = normalize_email(address) if "@" in (address or "") else normalize_phone(address) or address
    return bool(
        one(
            "SELECT id FROM suppressions WHERE workspace_id = ? AND address IN (?, ?) AND channel IN (?, 'all') LIMIT 1",
            (workspace_id, addr, (address or "").strip(), channel),
        )
    )


def add_suppression(workspace_id: str, address: str, channel: str = "all", reason: str = "unsubscribe") -> None:
    addr = normalize_email(address) if "@" in (address or "") else normalize_phone(address) or (address or "").strip()
    if not addr or is_suppressed(workspace_id, addr, channel):
        return
    insert("suppressions", {"address": addr, "channel": channel, "reason": reason}, workspace_id)


def mark_contacted(workspace_id: str, lead_id: str) -> None:
    if not lead_id:
        return
    lead = one("SELECT stage FROM leads WHERE id = ? AND workspace_id = ?", (lead_id, workspace_id))
    if not lead:
        return
    stage = lead["stage"] if lead["stage"] not in {"new", ""} else "contacted"
    execute(
        "UPDATE leads SET last_contacted_at = ?, stage = ?, status = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), stage, stage, lead_id, workspace_id),
    )


def wrap_email(body: str, message_id: str) -> tuple[str, str]:
    def replacer(match: re.Match) -> str:
        raw = match.group(1).rstrip(").,]")
        return f"{public_url()}/t/c/{message_id}?u={quote(raw, safe='')}"

    tracked = LINK_RE.sub(replacer, body or "")
    html = (
        "<div style='font-family:Georgia,serif;font-size:16px;line-height:1.5'>"
        + escape(tracked).replace("\n", "<br>")
        + f"<img src='{public_url()}/t/o/{message_id}.gif' width='1' height='1' alt='' /></div>"
    )
    return body or "", html


def render_copy(text: str, lead: dict) -> str:
    name = lead.get("name") or "there"
    return (
        (text or "")
        .replace("{{name}}", name)
        .replace("{name}", name)
        .replace("{username}", name)
        .replace("{{email}}", lead.get("email") or "")
        .replace("{{phone}}", lead.get("phone") or "")
    )


def ai_write(workspace_id: str, channel: str, instruction: str, lead: dict, thread: list[dict]) -> str:
    key = setting(workspace_id, "xai_api_key", env("XAI_API_KEY"))
    model = setting(workspace_id, "xai_model", env("XAI_MODEL", "grok-4"))
    url = "https://api.x.ai/v1/chat/completions"
    if not key:
        key = setting(workspace_id, "openrouter_api_key", env("OPENROUTER_API_KEY"))
        model = setting(workspace_id, "openrouter_model", env("OPENROUTER_MODEL", "openrouter/free"))
        url = "https://openrouter.ai/api/v1/chat/completions"
    if not key:
        return ""
    payload = {
        "company": company_context(workspace_id),
        "channel": channel,
        "instruction": instruction,
        "lead": {"name": lead.get("name"), "phone": lead.get("phone"), "email": lead.get("email"), "stage": lead.get("stage")},
        "thread": thread[-8:],
        "rules": "No fake prices. SMS under 160 chars. Email may start with Subject: line. Sound human.",
    }
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.3,
                "messages": [
                    {"role": "system", "content": "Write only the outbound message. Use company documents as source of truth."},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
            },
            timeout=45,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"].strip()
    except (requests.RequestException, KeyError, ValueError):
        return ""


def parse_draft(channel: str, draft: str) -> tuple[str, str]:
    text = (draft or "").strip()
    if channel != "email" or not text:
        return "", text
    lines = text.splitlines()
    match = SUBJECT_RE.match(lines[0]) if lines else None
    if not match:
        return "", text
    body_lines = lines[1:]
    if body_lines and not body_lines[0].strip():
        body_lines = body_lines[1:]
    return match.group(1).strip(), "\n".join(body_lines).strip()


def send_email(workspace_id: str, to_addr: str, subject: str, body: str, lead_id: str = "", sequence_id: str = "", step_id: str = "", account_id: str = "") -> bool:
    row = one("SELECT * FROM email_connections WHERE id = ? AND workspace_id = ?", (account_id, workspace_id)) if account_id else one("SELECT * FROM email_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
    conn = reveal("email_connections", row)
    user = (conn or {}).get("email_address") or env("SMTP_USER", "stock360snoreply@gmail.com")
    password = (conn or {}).get("secret") if account_id else (conn or {}).get("secret") or env("SMTP_PASSWORD")
    host = (conn or {}).get("smtp_host") or env("SMTP_HOST", "smtp.gmail.com")
    port = int((conn or {}).get("smtp_port") or env("SMTP_PORT", "587") or 587)
    cap = int((conn or {}).get("daily_cap") or env("EMAIL_DEFAULT_DAILY_CAP", "500") or 500)
    common = {
        "channel": "email",
        "direction": "outbound",
        "recipient": to_addr,
        "lead_id": lead_id,
        "sequence_id": sequence_id,
        "step_id": step_id,
        "account_id": account_id,
        "subject": subject,
        "body": body,
    }
    if account_id and not row:
        insert("messages", {**common, "status": "account_unavailable"}, workspace_id)
        return False
    sent = one(
        "SELECT COUNT(*) AS total FROM messages WHERE workspace_id = ? AND channel = 'email' AND direction = 'outbound' AND status IN ('sent', 'sandbox') AND created_at LIKE ?",
        (workspace_id, utc_today() + "%"),
    )
    if is_suppressed(workspace_id, to_addr, "email"):
        insert("messages", {**common, "status": "suppressed"}, workspace_id)
        return False
    if int((sent or {}).get("total") or 0) >= cap:
        insert("messages", {**common, "status": "limit_exceeded"}, workspace_id)
        return False
    if not user or not password:
        insert("messages", {**common, "status": "sandbox"}, workspace_id)
        mark_contacted(workspace_id, lead_id)
        return True
    message_id = new_id()
    plain, html = wrap_email(body, message_id)
    message = EmailMessage()
    message["Subject"] = subject or "Hello"
    message["From"] = user
    message["To"] = to_addr
    message["List-Unsubscribe"] = f"<mailto:{user}?subject=unsubscribe>"
    message.set_content(plain)
    message.add_alternative(html, subtype="html")
    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(message)
        insert("messages", {**common, "id": message_id, "status": "sent"}, workspace_id)
        mark_contacted(workspace_id, lead_id)
        return True
    except (OSError, smtplib.SMTPException):
        insert("messages", {**common, "status": "failed"}, workspace_id)
        return False


def send_sms(workspace_id: str, phone: str, body: str, lead_id: str = "", sequence_id: str = "", step_id: str = "", account_id: str = "") -> bool:
    row = one("SELECT * FROM sms_connections WHERE id = ? AND workspace_id = ?", (account_id, workspace_id)) if account_id else one("SELECT * FROM sms_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
    conn = reveal("sms_connections", row)
    key = (conn or {}).get("secret") if account_id else (conn or {}).get("secret") or env("MSG91_AUTH_KEY")
    sender = (conn or {}).get("sender_id") if account_id else (conn or {}).get("sender_id") or env("SMS_DEFAULT_SENDER")
    template_id = (conn or {}).get("template_id") or ""
    common = {
        "channel": "sms",
        "direction": "outbound",
        "recipient": phone,
        "lead_id": lead_id,
        "sequence_id": sequence_id,
        "step_id": step_id,
        "account_id": account_id,
        "body": body,
    }
    if account_id and not row:
        insert("messages", {**common, "status": "account_unavailable"}, workspace_id)
        return False
    if is_suppressed(workspace_id, phone, "sms"):
        insert("messages", {**common, "status": "suppressed"}, workspace_id)
        return False
    if not key or not sender:
        insert("messages", {**common, "status": "sandbox"}, workspace_id)
        mark_contacted(workspace_id, lead_id)
        return True
    if not template_id:
        insert("messages", {**common, "status": "needs_template"}, workspace_id)
        return False
    try:
        response = requests.post(
            "https://control.msg91.com/api/v5/flow/",
            headers={"authkey": key, "Content-Type": "application/json"},
            json={"template_id": template_id, "sender": sender, "short_url": "0", "recipients": [{"mobiles": normalize_phone(phone), "VAR1": body}]},
            timeout=15,
        )
        ok = response.ok
    except requests.RequestException:
        ok = False
    insert("messages", {**common, "status": "sent" if ok else "failed"}, workspace_id)
    if ok:
        mark_contacted(workspace_id, lead_id)
    return ok


def send_whatsapp(workspace_id: str, phone: str, body: str, lead_id: str = "", sequence_id: str = "", step_id: str = "", account_id: str = "") -> bool:
    row = one("SELECT * FROM whatsapp_connections WHERE id = ? AND workspace_id = ?", (account_id, workspace_id)) if account_id else one("SELECT * FROM whatsapp_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
    conn = reveal("whatsapp_connections", row)
    token = (conn or {}).get("access_token", "") if account_id else env("WHATSAPP_TOKEN") or env("META_ACCESS_TOKEN") or (conn or {}).get("access_token", "")
    phone_id = (conn or {}).get("phone_number_id", "") if account_id else env("WHATSAPP_PHONE_NUMBER_ID") or (conn or {}).get("phone_number_id", "")
    common = {
        "channel": "whatsapp",
        "direction": "outbound",
        "recipient": phone,
        "lead_id": lead_id,
        "sequence_id": sequence_id,
        "step_id": step_id,
        "account_id": account_id,
        "body": body,
    }
    if account_id and not row:
        insert("messages", {**common, "status": "account_unavailable"}, workspace_id)
        return False
    if is_suppressed(workspace_id, phone, "whatsapp"):
        insert("messages", {**common, "status": "suppressed"}, workspace_id)
        return False
    if not token or not phone_id:
        insert("messages", {**common, "status": "sandbox"}, workspace_id)
        mark_contacted(workspace_id, lead_id)
        return True
    version = env("META_GRAPH_API_VERSION", "v25.0")
    try:
        response = requests.post(
            f"https://graph.facebook.com/{version}/{phone_id}/messages",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"messaging_product": "whatsapp", "to": phone, "type": "text", "text": {"body": body}},
            timeout=15,
        )
        ok = response.ok
    except requests.RequestException:
        ok = False
    insert("messages", {**common, "status": "sent" if ok else "failed"}, workspace_id)
    if ok:
        mark_contacted(workspace_id, lead_id)
    return ok


def send_channel(workspace_id: str, channel: str, lead: dict, subject: str, body: str, sequence_id: str = "", step_id: str = "", account_id: str = "") -> bool:
    extra = {"lead_id": lead.get("id", ""), "sequence_id": sequence_id, "step_id": step_id, "account_id": account_id}
    if channel == "email":
        return send_email(workspace_id, lead.get("email") or "", subject, body, **extra)
    if channel == "sms":
        return send_sms(workspace_id, lead.get("phone") or "", body, **extra)
    return send_whatsapp(workspace_id, lead.get("phone") or "", body, **extra)


def step_copy(workspace_id: str, step: dict, lead: dict) -> tuple[str, str]:
    thread = query(
        "SELECT direction, body FROM messages WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at ASC LIMIT 12",
        (workspace_id, lead.get("id")),
    )
    if step.get("mode") == "ai":
        draft = ai_write(workspace_id, step["channel"], step.get("body") or "Write the next follow-up.", lead, thread)
        if step["channel"] == "email":
            subject, body = parse_draft("email", draft)
            return subject or step.get("subject") or "Hello", body or draft or render_copy(step.get("body") or "Hi {{name}}, just checking in.", lead)
        return step.get("subject") or "Hello", draft or render_copy(step.get("body") or "Hi {{name}}, just checking in.", lead)
    return render_copy(step.get("subject") or "Hello", lead), render_copy(step.get("body") or "", lead)


def _step_variants(step: dict) -> list[dict]:
    """[baseline, *alternatives]. The baseline is the step's own subject/body and is always arm 0."""
    variants = [{"subject": step.get("subject") or "", "body": step.get("body") or ""}]
    raw = step.get("variants")
    try:
        loaded = json.loads(raw) if isinstance(raw, str) and raw.strip() else (raw or [])
    except (TypeError, json.JSONDecodeError):
        loaded = []
    for item in loaded if isinstance(loaded, list) else []:
        if isinstance(item, dict) and str(item.get("body") or "").strip():
            variants.append({"subject": str(item.get("subject") or ""), "body": str(item["body"]).strip()})
    return variants


def step_copy_learned(workspace_id: str, step: dict, lead: dict, sequence_id: str) -> tuple[str, str, str]:
    """Returns (subject, body, decision_id). decision_id is '' when nothing was learned from this send."""
    variants = _step_variants(step) if step.get("mode") != "ai" else []
    if len(variants) > 1 and delivery_mode(workspace_id, step["channel"]) == "live":
        # Only learn from real deliveries: sandbox sends never get replies and would teach the system nothing true.
        by_key = {learning.variant_key(v["subject"], v["body"]): v for v in variants}
        picked = _safe(
            learning.choose,
            workspace_id,
            f"seq:{sequence_id}:{step['step_order']}",
            list(by_key),
            str(lead.get("source") or "unknown")[:40],
            lead.get("id") or "",
        )
        if picked:
            arm, decision_id = picked
            chosen = by_key[arm]
            return render_copy(chosen["subject"] or "Hello", lead), render_copy(chosen["body"], lead), decision_id
    subject, body = step_copy(workspace_id, step, lead)
    return subject, body, ""


def matching_sequences(workspace_id: str, channel: str, new_lead: bool) -> list[dict]:
    rows = query("SELECT * FROM sequences WHERE workspace_id = ? AND enabled = 1", (workspace_id,))
    out = []
    for row in rows:
        if new_lead and row.get("trigger_new_lead"):
            out.append(row)
            continue
        if channel == "whatsapp" and row.get("trigger_whatsapp"):
            out.append(row)
        elif channel == "sms" and row.get("trigger_sms"):
            out.append(row)
        elif channel == "email" and row.get("trigger_email"):
            out.append(row)
    return out


def enroll(workspace_id: str, sequence_id: str, lead_id: str) -> str | None:
    existing = one(
        "SELECT id FROM sequence_enrollments WHERE workspace_id = ? AND sequence_id = ? AND lead_id = ? AND status = 'active'",
        (workspace_id, sequence_id, lead_id),
    )
    if existing:
        return existing["id"]
    first = one(
        "SELECT * FROM sequence_steps WHERE workspace_id = ? AND sequence_id = ? ORDER BY step_order ASC LIMIT 1",
        (workspace_id, sequence_id),
    )
    if not first:
        return None
    return insert(
        "sequence_enrollments",
        {
            "sequence_id": sequence_id,
            "lead_id": lead_id,
            "current_step": 0,
            "status": "active",
            "next_run_at": hours_from_now(first["delay_hours"]),
        },
        workspace_id,
    )


def stop_sequences(workspace_id: str, lead_id: str, reason: str = "replied") -> None:
    if not lead_id:
        return
    status = reason if reason in {"replied", "stopped", "unsubscribed"} else "replied"
    execute(
        "UPDATE sequence_enrollments SET status = ? WHERE workspace_id = ? AND lead_id = ? AND status = 'active'",
        (status, workspace_id, lead_id),
    )


def execute_event(workspace_id: str, event_type: str, payload: dict) -> int:
    insert("events", {"event_type": event_type, "payload": json_text(payload)}, workspace_id)
    rules = query(
        "SELECT * FROM automations WHERE workspace_id = ? AND enabled = 1 AND trigger_type = ?",
        (workspace_id, event_type),
    )
    actions = 0
    for rule in rules:
        keyword = (rule["keyword"] or "").lower().strip()
        body = str(payload.get("text", "")).lower()
        if keyword and keyword not in body:
            continue
        delivered = True
        detail = rule["action_value"]
        if rule["action_type"] == "enroll in sequence":
            lead_id = payload.get("lead_id")
            if lead_id:
                enrollment_id = enroll(workspace_id, rule["action_value"], str(lead_id))
                if not enrollment_id:
                    seq = one(
                        "SELECT id FROM sequences WHERE workspace_id = ? AND name = ? LIMIT 1",
                        (workspace_id, rule["action_value"]),
                    )
                    enrollment_id = enroll(workspace_id, seq["id"], str(lead_id)) if seq else None
                delivered = bool(enrollment_id)
                detail = "Enrolled" if delivered else "Could not enroll"
            else:
                delivered = False
                detail = "No lead to enroll"
        elif rule["action_type"] == "create lead":
            find_or_create_lead(
                workspace_id,
                str(payload.get("name", "")),
                str(payload.get("recipient", "") if "@" not in str(payload.get("recipient", "")) else ""),
                str(payload.get("recipient", "") if "@" in str(payload.get("recipient", "")) else ""),
                source=str(payload.get("channel", "automation")),
            )
        elif rule["action_type"] in {"send DM", "send WhatsApp message"} and payload.get("recipient"):
            delivered = send_whatsapp(workspace_id, str(payload["recipient"]), rule["action_value"], payload.get("lead_id") or "")
        elif rule["action_type"] == "send email" and payload.get("recipient"):
            delivered = send_email(workspace_id, str(payload["recipient"]), "Hello", rule["action_value"], payload.get("lead_id") or "")
        elif rule["action_type"] == "send SMS" and payload.get("recipient"):
            delivered = send_sms(workspace_id, str(payload["recipient"]), rule["action_value"], payload.get("lead_id") or "")
        execute(
            "UPDATE automations SET executions = executions + 1, updated_at = ? WHERE workspace_id = ? AND id = ?",
            (now_iso(), workspace_id, rule["id"]),
        )
        insert(
            "automation_runs",
            {"automation_id": rule["id"], "action_type": rule["action_type"], "status": "sent" if delivered else "failed", "detail": detail},
            workspace_id,
        )
        actions += 1
    return actions


def process_inbound(workspace_id: str, channel: str, sender: str, body: str, name: str = "") -> dict:
    phone = sender if "@" not in sender else ""
    email = sender if "@" in sender else ""
    lead, created = find_or_create_lead(workspace_id, name, phone, email, source=channel)
    lead_id = lead.get("id", "")
    unsubscribed = is_unsubscribe(body)
    intent = ""
    if unsubscribed:
        add_suppression(workspace_id, sender, channel, "stop")
        if lead_id:
            execute("UPDATE leads SET stage = 'unsubscribed', status = 'unsubscribed' WHERE id = ? AND workspace_id = ?", (lead_id, workspace_id))
            stop_sequences(workspace_id, lead_id, "unsubscribed")
        _safe(learning.record_outcome, workspace_id, lead_id, "unsubscribe")
        status = "unsubscribed"
    else:
        if lead.get("last_contacted_at"):
            execute("UPDATE leads SET stage = 'replied', status = 'replied' WHERE id = ? AND workspace_id = ?", (lead_id, workspace_id))
            stop_sequences(workspace_id, lead_id, "replied")
            intent, _confidence = _safe(learning.classify, workspace_id, body, default=("neutral", 0.0))
            _safe(learning.record_outcome, workspace_id, lead_id, f"reply_{intent}")
        status = "received"
    insert(
        "messages",
        {"channel": channel, "direction": "inbound", "recipient": sender, "lead_id": lead_id, "body": body, "status": status, "intent": intent},
        workspace_id,
    )
    event_name = "new DM received" if channel != "email" else "inbound_message"
    actions = execute_event(workspace_id, event_name, {"channel": channel, "recipient": sender, "text": body, "lead_id": lead_id})
    enrolled = 0
    if not unsubscribed:
        for book in matching_sequences(workspace_id, channel, created):
            if enroll(workspace_id, book["id"], lead_id):
                enrolled += 1
        if created:
            actions += execute_event(workspace_id, "new lead received", {"recipient": sender, "text": body, "lead_id": lead_id, "channel": channel})
    return {"lead_id": lead_id, "created": created, "unsubscribed": unsubscribed, "intent": intent, "enrolled": enrolled, "actions": actions}


def claim_due_enrollments(workspace_id: str | None = None, limit: int = 80) -> list[dict]:
    scope, scope_params = ("AND workspace_id = ?", (workspace_id,)) if workspace_id else ("", ())
    # Rows left in 'processing' by a crashed run are returned to the queue after an hour.
    execute(
        f"UPDATE sequence_enrollments SET status = 'active' WHERE status = 'processing' AND next_run_at <= ? {scope}",
        (hours_from_now(-1), *scope_params),
    )
    due = query(
        f"SELECT id FROM sequence_enrollments WHERE status = 'active' AND next_run_at <= ? {scope} ORDER BY next_run_at ASC LIMIT ?",
        (now_iso(), *scope_params, limit),
    )
    ids = [row["id"] for row in due]
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    # Only the rows we selected are claimed; the rest stay 'active' for the next run.
    execute(f"UPDATE sequence_enrollments SET status = 'processing' WHERE status = 'active' AND id IN ({marks})", tuple(ids))
    return query(f"SELECT * FROM sequence_enrollments WHERE status = 'processing' AND id IN ({marks}) ORDER BY next_run_at ASC", tuple(ids))


def process_due(workspace_id: str | None = None, limit: int = 80) -> dict:
    due = claim_due_enrollments(workspace_id, limit)
    sent = failed = skipped = stopped = 0
    for enrollment in due:
        ws = enrollment["workspace_id"]
        book = one("SELECT * FROM sequences WHERE id = ? AND workspace_id = ?", (enrollment["sequence_id"], ws))
        lead = one("SELECT * FROM leads WHERE id = ? AND workspace_id = ?", (enrollment["lead_id"], ws))
        if not book or not book["enabled"] or not lead:
            execute("UPDATE sequence_enrollments SET status = 'stopped' WHERE id = ? AND workspace_id = ?", (enrollment["id"], ws))
            stopped += 1
            continue
        if book["stop_on_reply"]:
            inbound = one(
                "SELECT id FROM messages WHERE workspace_id = ? AND lead_id = ? AND direction = 'inbound' AND created_at >= ? LIMIT 1",
                (ws, lead["id"], enrollment["created_at"]),
            )
            if inbound and lead.get("last_contacted_at"):
                execute("UPDATE sequence_enrollments SET status = 'replied' WHERE id = ? AND workspace_id = ?", (enrollment["id"], ws))
                stopped += 1
                continue
        nxt = int(enrollment["current_step"]) + 1
        step = one("SELECT * FROM sequence_steps WHERE workspace_id = ? AND sequence_id = ? AND step_order = ?", (ws, book["id"], nxt))
        if not step:
            execute("UPDATE sequence_enrollments SET status = 'completed', current_step = ? WHERE id = ? AND workspace_id = ?", (enrollment["current_step"], enrollment["id"], ws))
            stopped += 1
            continue
        recipient = lead["email"] if step["channel"] == "email" else lead["phone"]
        if not recipient:
            skipped += 1
            execute("UPDATE sequence_enrollments SET current_step = ?, next_run_at = ?, status = 'active' WHERE id = ? AND workspace_id = ?", (nxt, hours_from_now(0.02), enrollment["id"], ws))
            continue
        if is_suppressed(ws, recipient, step["channel"]) or lead.get("stage") == "unsubscribed":
            execute("UPDATE sequence_enrollments SET status = 'unsubscribed' WHERE id = ? AND workspace_id = ?", (enrollment["id"], ws))
            stopped += 1
            continue
        subject, body, decision_id = step_copy_learned(ws, step, lead, book["id"])
        ok = send_channel(ws, step["channel"], lead, subject, body, book["id"], step["id"], step.get("account_id") or "")
        if decision_id and not ok:
            _safe(learning.void_decision, decision_id)  # a send that did not go out is not evidence about the copy
        if ok:
            sent += 1
            following = one("SELECT delay_hours FROM sequence_steps WHERE workspace_id = ? AND sequence_id = ? AND step_order = ?", (ws, book["id"], nxt + 1))
            if following:
                execute(
                    "UPDATE sequence_enrollments SET current_step = ?, last_sent_at = ?, next_run_at = ?, status = 'active' WHERE id = ? AND workspace_id = ?",
                    (nxt, now_iso(), hours_from_now(following["delay_hours"]), enrollment["id"], ws),
                )
            else:
                execute(
                    "UPDATE sequence_enrollments SET current_step = ?, last_sent_at = ?, status = 'completed' WHERE id = ? AND workspace_id = ?",
                    (nxt, now_iso(), enrollment["id"], ws),
                )
        else:
            failed += 1
            execute("UPDATE sequence_enrollments SET next_run_at = ?, status = 'active' WHERE id = ? AND workspace_id = ?", (hours_from_now(1), enrollment["id"], ws))
    return {"sent": sent, "failed": failed, "skipped": skipped, "stopped": stopped, "scanned": len(due)}


def process_due_campaigns(workspace_id: str | None = None, limit: int = 25) -> dict:
    conditions = ["status IN ('scheduled', 'running')"]
    params = []
    if workspace_id:
        conditions.append("workspace_id = ?")
        params.append(workspace_id)
    if conditions:
        conditions.append("(scheduled_at = '' OR scheduled_at <= ?)")
        params.append(now_iso())
    rows = query(
        f"SELECT * FROM campaigns WHERE {' AND '.join(conditions)} ORDER BY scheduled_at ASC, created_at ASC LIMIT ?",
        tuple(params + [limit]),
    )
    processed = 0
    for campaign in rows:
        ws = campaign["workspace_id"]
        filters = campaign.get("audience_filters") or "{}"
        try:
            audience_filters = json.loads(filters)
        except (TypeError, json.JSONDecodeError):
            audience_filters = {}
        account_ids = json.loads(campaign.get("account_ids") or "[]") if isinstance(campaign.get("account_ids"), str) else (campaign.get("account_ids") or [])
        if not isinstance(account_ids, list):
            account_ids = []
        channels = json.loads(campaign.get("channels") or "[]") if isinstance(campaign.get("channels"), str) else (campaign.get("channels") or [])
        if not isinstance(channels, list):
            channels = []
        if not channels:
            execute("UPDATE campaigns SET status = 'completed', updated_at = ? WHERE id = ? AND workspace_id = ?", (now_iso(), campaign["id"], ws))
            continue
        conditions = ["workspace_id = ?"]
        values = [ws]
        for key, column in (("stage", "stage"), ("source", "source")):
            value = str(audience_filters.get(key) or "").strip()
            if value:
                conditions.append(f"{column} = ?")
                values.append(value)
        tag = str(audience_filters.get("tag") or "").strip()
        if tag:
            conditions.append("tags LIKE ?")
            values.append(f'%"{tag}"%')
        if audience_filters.get("has_email") is True:
            conditions.append("email <> ''")
        elif audience_filters.get("has_email") is False:
            conditions.append("email = ''")
        if audience_filters.get("has_phone") is True:
            conditions.append("phone <> ''")
        elif audience_filters.get("has_phone") is False:
            conditions.append("phone = ''")
        leads = query(f"SELECT * FROM leads WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT 200", tuple(values))
        execute("UPDATE campaigns SET status = 'running', updated_at = ? WHERE id = ? AND workspace_id = ?", (now_iso(), campaign["id"], ws))
        content = campaign.get("content") or {}
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                content = {}
        for lead in leads:
            for channel in channels:
                if not account_ids:
                    continue
                body = str((content.get(channel) if isinstance(content, dict) else {}).get("body") or content.get("body") or "")
                subject = str((content.get(channel) if isinstance(content, dict) else {}).get("subject") or content.get("subject") or "").strip()
                if not body:
                    body = "Hi {{name}}, we’re reaching out."
                rendered_subject = render_copy(subject, lead)
                rendered_body = render_copy(body, lead)
                ok = send_channel(ws, channel, lead, rendered_subject, rendered_body, account_id=(account_ids[0] if account_ids else ""))
                if ok:
                    insert(
                        "messages",
                        {"workspace_id": ws, "channel": channel, "direction": "outbound", "recipient": lead.get("email") if channel == "email" else lead.get("phone") or "", "lead_id": lead.get("id"), "campaign_id": campaign["id"], "subject": rendered_subject, "body": rendered_body, "status": "sent"},
                        ws,
                    )
                    processed += 1
        execute("UPDATE campaigns SET status = 'completed', updated_at = ? WHERE id = ? AND workspace_id = ?", (now_iso(), campaign["id"], ws))
    return {"processed": processed, "campaigns": len(rows)}


def extract_text(filename: str, data: bytes) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md", ".csv"}:
        return data.decode("utf-8", errors="ignore")[:20000]
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader
            import io

            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages)[:20000]
        except Exception:
            return ""
    return ""


def delivery_mode(workspace_id: str, channel: str) -> str:
    if channel == "email":
        conn = one("SELECT secret, status FROM email_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
        if (conn or {}).get("secret") or env("SMTP_PASSWORD"):
            return "live"
        return "sandbox"
    if channel == "sms":
        conn = one("SELECT secret, sender_id FROM sms_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
        if ((conn or {}).get("secret") and (conn or {}).get("sender_id")) or (env("MSG91_AUTH_KEY") and env("SMS_DEFAULT_SENDER")):
            return "live"
        return "sandbox"
    conn = one("SELECT access_token, phone_number_id FROM whatsapp_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (workspace_id,))
    if (env("WHATSAPP_TOKEN") and env("WHATSAPP_PHONE_NUMBER_ID")) or ((conn or {}).get("access_token") and (conn or {}).get("phone_number_id")):
        return "live"
    return "sandbox"


# --- Autonomous orchestration (LLM plans; deterministic engine executes) ---

REQUIRED_FIELDS = (
    "objective",
    "audience",
    "geography",
    "budget",
    "success_metric",
    "channels",
    "autonomy",
)

QUESTION_BANK = {
    "objective": {
        "prompt": "What should Revenue360 optimize for?",
        "options": [
            {"id": "visits", "label": "Website visits"},
            {"id": "whatsapp", "label": "WhatsApp conversations"},
            {"id": "demos", "label": "Demo bookings"},
            {"id": "trials", "label": "Free trials"},
            {"id": "paid", "label": "Paid customers"},
        ],
    },
    "audience": {
        "prompt": "Who should we target?",
        "options": [
            {"id": "smb", "label": "Small businesses"},
            {"id": "consumers", "label": "Consumers"},
            {"id": "both", "label": "Both"},
            {"id": "partners", "label": "Partners"},
            {"id": "existing", "label": "Existing customers"},
        ],
    },
    "geography": {
        "prompt": "Where should the system operate?",
        "options": [
            {"id": "india", "label": "India"},
            {"id": "us", "label": "US"},
            {"id": "uk", "label": "UK"},
            {"id": "global", "label": "Global"},
        ],
    },
    "budget": {
        "prompt": "What daily budget should the system stay within?",
        "options": [
            {"id": "500", "label": "₹500/day"},
            {"id": "1000", "label": "₹1,000/day"},
            {"id": "2500", "label": "₹2,500/day"},
            {"id": "5000", "label": "₹5,000/day"},
        ],
    },
    "success_metric": {
        "prompt": "How will we know this worked?",
        "options": [
            {"id": "qualified_leads", "label": "Qualified leads"},
            {"id": "replies", "label": "Replies"},
            {"id": "bookings", "label": "Bookings"},
            {"id": "paid", "label": "Paid customers"},
        ],
    },
    "channels": {
        "prompt": "Which connected channels can Revenue360 use?",
        "multi": True,
        "options": [
            {"id": "whatsapp", "label": "WhatsApp"},
            {"id": "email", "label": "Email"},
            {"id": "sms", "label": "SMS"},
            {"id": "instagram", "label": "Instagram"},
            {"id": "facebook", "label": "Facebook"},
            {"id": "meta_ads", "label": "Meta Ads"},
        ],
    },
    "autonomy": {
        "prompt": "How autonomous should this run be?",
        "options": [
            {"id": "approve_all", "label": "Everything requires approval"},
            {"id": "content_auto", "label": "Content automatic, ads require approval"},
            {"id": "spend_guard", "label": "Everything automatic except spending"},
            {"id": "full", "label": "Fully autonomous within limits"},
        ],
    },
}

DEFAULT_POLICY = {
    "content_creation": "auto",
    "content_publishing": "approval",
    "email_sending": "auto",
    "whatsapp_sending": "auto",
    "ad_publish": "approval",
    "ad_budget_change": "approval",
    "spend_limit_daily": 1000,
}

DEFAULT_REQUIREMENTS = {
    "objective": None,
    "business": None,
    "offer": None,
    "audience": None,
    "geography": None,
    "channels": [],
    "content_types": [],
    "budget": None,
    "schedule": None,
    "approval_policy": None,
    "brand_voice": None,
    "success_metric": None,
    "connected_accounts": [],
    "autonomy": None,
}


def _json_load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _connected_snapshot(workspace_id: str) -> list[dict]:
    from settings import list_accounts

    try:
        return list_accounts({"workspace_id": workspace_id})
    except Exception:
        return []


def _missing_requirements(req: dict) -> list[str]:
    missing = []
    for field in REQUIRED_FIELDS:
        value = req.get(field)
        if value in (None, "", [], {}):
            missing.append(field)
    return missing


def _apply_choice(req: dict, field: str, value):
    if field == "channels":
        if isinstance(value, list):
            req["channels"] = value
        else:
            current = list(req.get("channels") or [])
            text = str(value)
            if text not in current:
                current.append(text)
            req["channels"] = current
    elif field == "budget":
        mapping = {"500": 500, "1000": 1000, "2500": 2500, "5000": 5000}
        req["budget"] = mapping.get(str(value), value if str(value).isdigit() else value)
    else:
        req[field] = value
    return req


def _policy_from_autonomy(autonomy: str, budget) -> dict:
    policy = dict(DEFAULT_POLICY)
    try:
        policy["spend_limit_daily"] = int(budget or 1000)
    except (TypeError, ValueError):
        policy["spend_limit_daily"] = 1000
    if autonomy == "approve_all":
        policy.update({"content_creation": "approval", "content_publishing": "approval", "email_sending": "approval", "whatsapp_sending": "approval", "ad_publish": "approval"})
    elif autonomy == "content_auto":
        policy.update({"content_creation": "auto", "content_publishing": "auto", "ad_publish": "approval"})
    elif autonomy == "spend_guard":
        policy.update({"content_creation": "auto", "content_publishing": "auto", "ad_publish": "auto", "ad_budget_change": "approval"})
    elif autonomy == "full":
        policy.update({"content_creation": "auto", "content_publishing": "auto", "ad_publish": "auto", "ad_budget_change": "auto"})
    return policy


def _build_workflow(req: dict, accounts: list[dict]) -> dict:
    channels = req.get("channels") or []
    account_ids = [a["id"] for a in accounts if a.get("platform") in channels or a.get("account_type") in channels]
    steps = [
        {"tool": "research_audience", "status": "pending"},
        {"tool": "generate_content", "status": "pending"},
        {"tool": "publish", "status": "pending", "channels": [c for c in channels if c in {"instagram", "facebook", "linkedin", "x", "tiktok", "youtube"}]},
        {"tool": "launch_ads", "status": "pending"} if "meta_ads" in channels else None,
        {"tool": "outreach", "status": "pending", "channels": [c for c in channels if c in {"email", "sms", "whatsapp"}]},
        {"tool": "measure", "status": "pending"},
        {"tool": "optimize", "status": "pending"},
    ]
    return {
        "goal": req.get("objective") or "grow",
        "steps": [s for s in steps if s],
        "account_ids": account_ids,
        "budget_daily": req.get("budget"),
        "success_metric": req.get("success_metric"),
        "geography": req.get("geography"),
        "audience": req.get("audience"),
    }


def _load_run(workspace_id: str, run_id: str | None = None) -> dict | None:
    if run_id:
        return one("SELECT * FROM agent_runs WHERE id = ? AND workspace_id = ?", (run_id, workspace_id))
    return one("SELECT * FROM agent_runs WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 1", (workspace_id,))


def _save_run(workspace_id: str, run: dict) -> str:
    values = {
        "title": run.get("title") or "New goal",
        "phase": run.get("phase") or "discovery",
        "status": run.get("status") or "draft",
        "requirements": json_text(run.get("requirements") or DEFAULT_REQUIREMENTS),
        "conversation": json_text(run.get("conversation") or []),
        "workflow": json_text(run.get("workflow") or {}),
        "policy": json_text(run.get("policy") or DEFAULT_POLICY),
        "autonomy": json_text(run.get("autonomy") or {}),
        "missing": json_text(run.get("missing") or []),
        "events": json_text(run.get("events") or []),
        "metrics": json_text(run.get("metrics") or {}),
        "account_ids": json_text(run.get("account_ids") or []),
        "approval_status": run.get("approval_status") or "",
        "last_question": json_text(run.get("last_question") or {}),
        "updated_at": now_iso(),
    }
    if run.get("id"):
        assignments = ", ".join(f"{k} = ?" for k in values)
        execute(f"UPDATE agent_runs SET {assignments} WHERE id = ? AND workspace_id = ?", (*values.values(), run["id"], workspace_id))
        return run["id"]
    return insert("agent_runs", values, workspace_id)


def _public_run(row: dict) -> dict:
    if not row:
        return {}
    return {
        "id": row["id"],
        "title": row.get("title") or "",
        "phase": row.get("phase"),
        "status": row.get("status"),
        "requirements": _json_load(row.get("requirements"), dict(DEFAULT_REQUIREMENTS)),
        "conversation": _json_load(row.get("conversation"), []),
        "workflow": _json_load(row.get("workflow"), {}),
        "policy": _json_load(row.get("policy"), dict(DEFAULT_POLICY)),
        "missing": _json_load(row.get("missing"), []),
        "metrics": _json_load(row.get("metrics"), {}),
        "account_ids": _json_load(row.get("account_ids"), []),
        "approval_status": row.get("approval_status") or "",
        "last_question": _json_load(row.get("last_question"), {}),
        "updated_at": row.get("updated_at"),
    }


def _infer_from_text(text: str, req: dict) -> dict:
    lower = (text or "").lower()
    if not req.get("objective"):
        if "whatsapp" in lower:
            req["objective"] = "whatsapp"
        elif "demo" in lower:
            req["objective"] = "demos"
        elif "paid" in lower or "customer" in lower or "sale" in lower:
            req["objective"] = "paid"
        elif "lead" in lower:
            req["objective"] = "visits"
    if not req.get("geography") and "india" in lower:
        req["geography"] = "india"
    if not req.get("audience"):
        if "saas" in lower or "business" in lower:
            req["audience"] = "smb"
        elif "partner" in lower:
            req["audience"] = "partners"
    if not req.get("channels"):
        guessed = [c for c in ("whatsapp", "email", "instagram", "facebook", "meta_ads") if c.replace("_", " ") in lower or c in lower]
        if guessed:
            req["channels"] = guessed
    return req


def process_agent_due(workspace_id: str | None = None) -> dict:
    """Deterministic execution of confirmed agent workflows. LLM is not called here."""
    scope = "AND workspace_id = ?" if workspace_id else ""
    params = (workspace_id,) if workspace_id else ()
    rows = query(
        f"SELECT * FROM agent_runs WHERE status = 'running' AND phase IN ('execution','observation','optimization') {scope} ORDER BY updated_at ASC LIMIT 20",
        params,
    )
    advanced = 0
    for row in rows:
        run = _public_run(row)
        workflow = run.get("workflow") or {}
        steps = workflow.get("steps") or []
        changed = False
        for step in steps:
            if step.get("status") == "pending":
                tool = step.get("tool")
                if tool == "outreach":
                    process_due(row["workspace_id"])
                elif tool == "launch_ads":
                    process_due_campaigns(row["workspace_id"])
                elif tool == "optimize":
                    try:
                        import learning as learning_mod

                        learning_mod.learn_nightly()
                    except Exception:
                        pass
                step["status"] = "complete"
                changed = True
                break
        if changed:
            all_done = all(s.get("status") == "complete" for s in steps)
            execute(
                "UPDATE agent_runs SET workflow = ?, phase = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
                (json_text(workflow), "observation" if all_done else "execution", now_iso(), row["id"], row["workspace_id"]),
            )
            advanced += 1
    return {"advanced": advanced, "scanned": len(rows)}


agent_router = APIRouter(prefix="/api/agent", tags=["agent"])


@agent_router.get("/home")
def agent_home(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    accounts = _connected_snapshot(ws)
    live = [a for a in accounts if a.get("status") in {"connected", "live"}]
    runs = query("SELECT id, title, phase, status, updated_at FROM agent_runs WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 20", (ws,))
    profile = one("SELECT company_name, offer FROM company_profiles WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)) or {}
    leads = one("SELECT COUNT(*) AS n FROM leads WHERE workspace_id = ?", (ws,))["n"]
    return {
        "accounts": accounts,
        "connected": len(live),
        "needs_connections": len(live) == 0,
        "company": profile,
        "leads": leads,
        "runs": runs,
        "ai": {"status": "ready", "router": "automatic", "primary": "deepseek-flash"},
    }


@agent_router.get("/runs")
def list_runs(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT id, title, phase, status, updated_at, created_at FROM agent_runs WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 50",
        (user["workspace_id"],),
    )


@agent_router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(user_from_header)) -> dict:
    row = _load_run(user["workspace_id"], run_id)
    if not row:
        raise HTTPException(404, "Workflow not found.")
    return _public_run(row)


@agent_router.post("/chat")
def agent_chat(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    text = str(payload.get("message") or payload.get("choice") or "").strip()
    field = str(payload.get("field") or "")
    row = _load_run(ws, payload.get("run_id"))
    run = _public_run(row) if row else {
        "title": (text[:80] if text else "New goal"),
        "phase": "discovery",
        "status": "draft",
        "requirements": dict(DEFAULT_REQUIREMENTS),
        "conversation": [],
        "workflow": {},
        "policy": dict(DEFAULT_POLICY),
        "missing": list(REQUIRED_FIELDS),
        "metrics": {},
        "account_ids": [],
        "approval_status": "",
        "last_question": {},
    }
    req = run.get("requirements") or dict(DEFAULT_REQUIREMENTS)
    conversation = run.get("conversation") or []
    if text:
        conversation.append({"role": "user", "text": text, "field": field, "at": now_iso()})
        if field:
            req = _apply_choice(req, field, payload.get("choices") if isinstance(payload.get("choices"), list) else payload.get("choice") or text)
        else:
            req = _infer_from_text(text, req)
            if not req.get("offer") and text and not field:
                req["offer"] = text[:240]
            if not run.get("title") or run.get("title") == "New goal":
                run["title"] = text[:80]
    missing = _missing_requirements(req)
    run["requirements"] = req
    run["missing"] = missing
    run["conversation"] = conversation
    if missing:
        nxt = missing[0]
        question = QUESTION_BANK[nxt]
        run["phase"] = "clarification"
        run["last_question"] = {"field": nxt, **question}
        conversation.append({"role": "agent", "text": question["prompt"], "field": nxt, "options": question["options"], "at": now_iso()})
        run["id"] = _save_run(ws, {**run, "id": run.get("id")})
        saved = _load_run(ws, run["id"])
        out = _public_run(saved)
        out["ask"] = run["last_question"]
        return out
    accounts = _connected_snapshot(ws)
    workflow = _build_workflow(req, accounts)
    policy = _policy_from_autonomy(str(req.get("autonomy") or "spend_guard"), req.get("budget"))
    run["phase"] = "confirmation"
    run["status"] = "ready"
    run["workflow"] = workflow
    run["policy"] = policy
    run["account_ids"] = workflow.get("account_ids") or []
    run["last_question"] = {}
    conversation.append({
        "role": "agent",
        "text": "Everything required is configured. Review the workflow, then start.",
        "at": now_iso(),
    })
    run["id"] = _save_run(ws, {**run, "id": run.get("id")})
    return _public_run(_load_run(ws, run["id"]))


@agent_router.post("/runs/{run_id}/start")
def start_run(run_id: str, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    row = _load_run(ws, run_id)
    if not row:
        raise HTTPException(404, "Workflow not found.")
    run = _public_run(row)
    if run.get("missing"):
        raise HTTPException(400, "Requirements are incomplete.")
    execute(
        "UPDATE agent_runs SET status = 'running', phase = 'execution', approval_status = 'approved', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), run_id, ws),
    )
    process_agent_due(ws)
    return {"ok": True, "phase": "execution"}


@agent_router.post("/runs/{run_id}/pause")
def pause_run(run_id: str, user: dict = Depends(user_from_header)) -> dict:
    execute(
        "UPDATE agent_runs SET status = 'paused', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), run_id, user["workspace_id"]),
    )
    return {"ok": True}


@agent_router.post("/connect/oauth")
def connect_oauth(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    """Primary connect path. Returns provider login URL when credentials exist; otherwise flags advanced setup."""
    provider = str(payload.get("provider") or "").lower()
    urls = {
        "gmail": "/api/auth/google-url",
        "google": "/api/auth/google-url",
        "google_ads": None,
        "meta": None,
        "whatsapp": None,
        "instagram": None,
        "facebook": None,
        "linkedin": None,
        "x": None,
        "tiktok": None,
        "youtube": None,
        "canva": None,
        "veed": None,
    }
    if provider in {"gmail", "google"}:
        from auth import google_url

        return {"method": "oauth", "provider": provider, **google_url()}
    configured = bool(env(f"{provider.upper()}_CLIENT_ID"))
    return {
        "method": "oauth" if configured else "advanced",
        "provider": provider,
        "url": None,
        "advanced": not configured,
        "message": "Continue with the provider when OAuth apps are configured, or use Advanced setup.",
    }


def _deepseek_plan(workspace_id: str, prompt: str) -> dict:
    """Workflow and copy only. DeepSeek does not generate pixels or video."""
    key = setting(workspace_id, "deepseek_api_key", env("DEEPSEEK_API_KEY")) or setting(workspace_id, "openrouter_api_key", env("OPENROUTER_API_KEY"))
    if not key:
        return {
            "provider": "builtin",
            "workflow": {
                "goal": prompt[:180],
                "steps": [
                    {"tool": "generate_image", "prompt": prompt},
                    {"tool": "generate_video", "prompt": prompt},
                    {"tool": "publish"},
                    {"tool": "measure"},
                ],
            },
            "note": "No DeepSeek key. Built-in plan used. Set DEEPSEEK_API_KEY for model planning.",
        }
    url = "https://api.deepseek.com/chat/completions"
    model = "deepseek-flash"
    if not setting(workspace_id, "deepseek_api_key", env("DEEPSEEK_API_KEY")):
        url = "https://openrouter.ai/api/v1/chat/completions"
        model = env("OPENROUTER_MODEL", "deepseek/deepseek-chat")
    try:
        response = requests.post(
            url,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "temperature": 0.2,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": "Return JSON only: {goal, steps:[{tool, prompt}]}. Tools allowed: research_audience, generate_image, generate_video, publish, launch_ads, outreach, measure, optimize."},
                    {"role": "user", "content": prompt[:2000]},
                ],
            },
            timeout=45,
        )
        response.raise_for_status()
        text = response.json()["choices"][0]["message"]["content"]
        return {"provider": model, "workflow": json.loads(text)}
    except (requests.RequestException, KeyError, ValueError, json.JSONDecodeError) as error:
        return {"provider": "builtin", "error": str(error)[:200], "workflow": {"goal": prompt[:180], "steps": [{"tool": "generate_image"}, {"tool": "generate_video"}, {"tool": "publish"}]}}


@agent_router.post("/content")
def generate_content(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    """Image and video work without Canva/VEED. Those accounts are optional overrides."""
    kind = str(payload.get("kind") or "image").lower()
    prompt = str(payload.get("prompt") or "").strip()
    prefer = str(payload.get("provider") or "auto").lower()
    if not prompt:
        raise HTTPException(400, "Prompt is required.")
    ws = user["workspace_id"]
    if kind == "workflow":
        return _deepseek_plan(ws, prompt)
    if kind == "image":
        if prefer == "canva":
            return {"provider": "canva", "status": "optional", "note": "Canva is optional. Built-in image still works if this account is not connected.", "url": ""}
        encoded = quote(prompt[:500])
        url = f"https://image.pollinations.ai/prompt/{encoded}?width=1024&height=1024&nologo=true&model=flux"
        return {"provider": "pollinations", "kind": "image", "url": url, "optional_accounts": ["canva"]}
    if kind == "video":
        if prefer == "veed":
            return {"provider": "veed", "status": "optional", "note": "VEED is optional. Built-in video still works without it."}
        pollen = setting(ws, "pollinations_api_key", env("POLLINATIONS_API_KEY"))
        encoded = quote(prompt[:400])
        if pollen:
            return {
                "provider": "pollinations",
                "kind": "video",
                "url": f"https://gen.pollinations.ai/video/{encoded}",
                "auth": "server",
                "optional_accounts": ["veed"],
            }
        return {
            "provider": "pollinations",
            "kind": "video",
            "url": f"https://gen.pollinations.ai/video/{encoded}",
            "note": "Video URL is ready. Anonymous video may be rate-limited; set POLLINATIONS_API_KEY or connect VEED to raise the cap.",
            "optional_accounts": ["veed"],
        }
    raise HTTPException(400, "kind must be image, video, or workflow.")