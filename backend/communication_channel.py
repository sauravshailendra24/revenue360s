from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from auth import user_from_header
import learning
from db import env, execute, insert, one, query, reveal, upsert, utc_today, public_url
from engine import delivery_mode, process_inbound, send_channel, send_email, send_sms, send_whatsapp

router = APIRouter(prefix="/api/email", tags=["email"])


@router.get("")
def email_page(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    conn = reveal("email_connections", one("SELECT * FROM email_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    cap = int((conn or {}).get("daily_cap") or env("EMAIL_DEFAULT_DAILY_CAP", "500") or 500)
    used = one(
        "SELECT COUNT(*) AS n FROM messages WHERE workspace_id = ? AND channel = 'email' AND direction = 'outbound' AND status IN ('sent', 'sandbox') AND created_at LIKE ?",
        (ws, utc_today() + "%"),
    )["n"]
    return {
        "connection": {
            "email_address": (conn or {}).get("email_address", "") or env("SMTP_USER", "stock360snoreply@gmail.com"),
            "smtp_host": (conn or {}).get("smtp_host", "smtp.gmail.com"),
            "smtp_port": (conn or {}).get("smtp_port", "587"),
            "daily_cap": cap,
            "status": (conn or {}).get("status", "sandbox"),
            "has_secret": bool((conn or {}).get("secret")),
        },
        "used": used,
        "mode": delivery_mode(ws, "email"),
        "templates": query("SELECT * FROM templates WHERE workspace_id = ? AND channel = 'email' ORDER BY created_at DESC", (ws,)),
        "leads": query("SELECT id, name, email FROM leads WHERE workspace_id = ? AND email != '' ORDER BY created_at DESC LIMIT 500", (ws,)),
        "log": query(
            "SELECT direction, recipient, subject, body, status, created_at FROM messages WHERE workspace_id = ? AND channel = 'email' ORDER BY created_at DESC LIMIT 100",
            (ws,),
        ),
    }


@router.post("/connection")
def save_connection(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    values = {
        "email_address": payload.get("email_address") or env("SMTP_USER", "stock360snoreply@gmail.com"),
        "smtp_host": payload.get("smtp_host") or "smtp.gmail.com",
        "smtp_port": str(payload.get("smtp_port") or "587"),
        "daily_cap": int(payload.get("daily_cap") or 500),
        "status": "connected" if payload.get("secret") or payload.get("email_address") else "sandbox",
    }
    if payload.get("secret"):
        values["secret"] = payload["secret"]
    upsert("email_connections", values, {"workspace_id": user["workspace_id"]})
    return {"ok": True}


@router.post("/send")
def compose_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    to_addr = (payload.get("to") or "").strip()
    body = (payload.get("body") or "").strip()
    if not to_addr or not body:
        raise HTTPException(400, "Recipient and body are required.")
    ok = send_email(user["workspace_id"], to_addr, payload.get("subject") or "Hello", body, payload.get("lead_id") or "", account_id=payload.get("account_id") or "")
    return {"ok": ok}


@router.post("/bulk")
def bulk_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    subject = payload.get("subject") or "Quick follow-up"
    body = payload.get("body") or ""
    sent = 0
    for item in payload.get("recipients") or []:
        addr = item.get("email") or ""
        name = item.get("name") or "there"
        if send_email(ws, addr, subject, body.replace("{{name}}", name), item.get("id") or ""):
            sent += 1
    return {"sent": sent}

email_router = router

router = APIRouter(prefix="/api/templates", tags=["templates"])


@router.get("")
def list_templates(channel: str | None = None, user: dict = Depends(user_from_header)) -> list:
    if channel:
        return query(
            "SELECT * FROM templates WHERE workspace_id = ? AND channel = ? ORDER BY created_at DESC",
            (user["workspace_id"], channel),
        )
    return query("SELECT * FROM templates WHERE workspace_id = ? ORDER BY created_at DESC", (user["workspace_id"],))


@router.post("")
def save_template(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    name = (payload.get("name") or "").strip()
    body = (payload.get("body") or "").strip()
    if not name or not body:
        raise HTTPException(400, "Name and body are required.")
    row_id = insert(
        "templates",
        {
            "name": name,
            "channel": payload.get("channel") or "whatsapp",
            "subject": payload.get("subject") or "",
            "body": body,
        },
        user["workspace_id"],
    )
    return {"id": row_id}


templates_router = router

router = APIRouter(prefix="/api/inbox", tags=["inbox"])


@router.get("")
def list_threads(user: dict = Depends(user_from_header)) -> list:
    return query(
        """
     SELECT l.id AS lead_id, l.name, l.email, l.phone, l.stage,
         MAX(m.created_at) AS last_at,
         GROUP_CONCAT(DISTINCT m.channel ORDER BY m.channel) AS channels,
         SUM(CASE WHEN m.direction='inbound' THEN 1 ELSE 0 END) AS inbound_count,
         SUM(CASE WHEN m.direction='outbound' THEN 1 ELSE 0 END) AS outbound_count
     FROM leads l
     JOIN messages m ON m.workspace_id = l.workspace_id AND
          (m.lead_id = l.id OR (m.lead_id = '' AND
        ((l.email <> '' AND m.recipient = l.email) OR (l.phone <> '' AND m.recipient = l.phone))))
     WHERE l.workspace_id = ?
     GROUP BY l.id, l.name, l.email, l.phone, l.stage
     ORDER BY last_at DESC LIMIT 80
        """,
        (user["workspace_id"],),
    )


@router.get("/thread")
def thread(lead_id: str, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    lead = one("SELECT * FROM leads WHERE id = ? AND workspace_id = ?", (lead_id, ws))
    if not lead:
        raise HTTPException(404, "Lead not found.")
    messages = query(
        "SELECT * FROM messages WHERE workspace_id = ? AND (lead_id = ? OR recipient IN (?, ?)) ORDER BY created_at ASC",
        (ws, lead_id, lead.get("email") or "", lead.get("phone") or ""),
    )
    return {"messages": messages, "lead": lead}


@router.post("/send")
def inbox_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    channel = payload.get("channel") or "whatsapp"
    recipient = payload.get("recipient") or ""
    lead_id = str(payload.get("lead_id") or "")
    body = (payload.get("body") or "").strip()
    if channel not in {"email", "sms", "whatsapp"} or not body:
        raise HTTPException(400, "Recipient and message required.")
    lead = one("SELECT * FROM leads WHERE id = ? AND workspace_id = ?", (lead_id, ws)) if lead_id else None
    if lead_id and not lead:
        raise HTTPException(404, "Lead not found.")
    if not lead:
        lead = one(
            "SELECT * FROM leads WHERE workspace_id = ? AND (email = ? OR phone = ? OR phone_normalized = ?) LIMIT 1",
            (ws, recipient, recipient, recipient[-10:] if recipient else ""),
        ) or {"email": recipient, "phone": recipient}
    recipient = lead.get("email") if channel == "email" else lead.get("phone")
    if not recipient:
        raise HTTPException(400, "This lead has no recipient for the selected channel.")
    ok = send_channel(ws, channel, lead, payload.get("subject") or "Re: your message", body, account_id=payload.get("account_id") or "")
    return {"ok": ok}


@router.post("/inbound")
def inbox_inbound(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    return process_inbound(
        user["workspace_id"],
        payload.get("channel") or "whatsapp",
        payload.get("sender") or "",
        payload.get("body") or "",
        payload.get("name") or "",
    )

@router.post("/intent")
def correct_intent(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    """Human correction of a reply's intent. This is training data for the reply classifier."""
    ws = user["workspace_id"]
    label = str(payload.get("label") or "")
    if label not in {"positive", "negative", "neutral"}:
        raise HTTPException(400, "Label must be positive, negative, or neutral.")
    message = one(
        "SELECT id, body, intent FROM messages WHERE id = ? AND workspace_id = ? AND direction = 'inbound'",
        (str(payload.get("message_id") or ""), ws),
    )
    if not message:
        raise HTTPException(404, "Inbound message not found.")
    if message.get("intent") != label:
        learning.train(ws, message.get("body") or "", label)
        execute("UPDATE messages SET intent = ? WHERE id = ? AND workspace_id = ?", (label, message["id"], ws))
    return {"ok": True}


inbox_router = router

router = APIRouter(prefix="/api/sms", tags=["sms"])


@router.get("")
def sms_page(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    conn = reveal("sms_connections", one("SELECT * FROM sms_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    return {
        "connection": {
            "sender_id": (conn or {}).get("sender_id", ""),
            "entity_id": (conn or {}).get("entity_id", ""),
            "template_id": (conn or {}).get("template_id", ""),
            "status": (conn or {}).get("status", "sandbox"),
            "has_secret": bool((conn or {}).get("secret")),
        },
        "mode": delivery_mode(ws, "sms"),
        "templates": query("SELECT * FROM templates WHERE workspace_id = ? AND channel = 'sms' ORDER BY created_at DESC", (ws,)),
        "leads": query("SELECT id, name, phone FROM leads WHERE workspace_id = ? AND phone != '' ORDER BY created_at DESC LIMIT 500", (ws,)),
        "log": query(
            "SELECT direction, recipient, body, status, created_at FROM messages WHERE workspace_id = ? AND channel = 'sms' ORDER BY created_at DESC LIMIT 100",
            (ws,),
        ),
    }


@router.post("/connection")
def save_connection(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    values = {
        "provider": payload.get("provider") or "msg91",
        "sender_id": payload.get("sender_id") or "",
        "entity_id": payload.get("entity_id") or "",
        "template_id": payload.get("template_id") or "",
        "status": "connected" if payload.get("secret") else "sandbox",
    }
    if payload.get("secret"):
        values["secret"] = payload["secret"]
    upsert("sms_connections", values, {"workspace_id": user["workspace_id"]})
    return {"ok": True}


@router.post("/send")
def compose_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    phone = (payload.get("phone") or "").strip()
    body = (payload.get("body") or "").strip()
    if not phone or not body:
        raise HTTPException(400, "Number and message are required.")
    return {"ok": send_sms(user["workspace_id"], phone, body, payload.get("lead_id") or "", account_id=payload.get("account_id") or "")}


@router.post("/bulk")
def bulk_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    sent = 0
    body = payload.get("body") or ""
    for item in payload.get("recipients") or []:
        if send_sms(user["workspace_id"], item.get("phone") or "", body.replace("{{name}}", item.get("name") or "there"), item.get("id") or ""):
            sent += 1
    return {"sent": sent}


@router.post("/inbound")
def record_inbound(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    return process_inbound(user["workspace_id"], "sms", payload.get("sender") or "", payload.get("body") or "")

import requests

sms_router = router

router = APIRouter(prefix="/api/whatsapp", tags=["whatsapp"])


@router.get("")
def whatsapp_page(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    conn = reveal(
        "whatsapp_connections",
        one("SELECT * FROM whatsapp_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)),
    )
    return {
        "connection": {
            "phone_number_id": (conn or {}).get("phone_number_id", "") or env("WHATSAPP_PHONE_NUMBER_ID"),
            "business_account_id": (conn or {}).get("business_account_id", "") or env("WHATSAPP_BUSINESS_ACCOUNT_ID"),
            "status": (conn or {}).get("status", "sandbox"),
            "has_token": bool((conn or {}).get("access_token") or env("WHATSAPP_TOKEN")),
        },
        "mode": delivery_mode(ws, "whatsapp"),
        "webhook": f"{public_url()}/webhooks/meta",
        "verify_token": env("META_WEBHOOK_VERIFY_TOKEN", "revenue360s-verify"),
        "templates": query("SELECT * FROM templates WHERE workspace_id = ? AND channel = 'whatsapp' ORDER BY created_at DESC", (ws,)),
        "connections": query(
            "SELECT phone_number_id, business_account_id, status, created_at FROM whatsapp_connections WHERE workspace_id = ? ORDER BY created_at DESC",
            (ws,),
        ),
        "log": query(
            "SELECT direction, recipient, body, status, created_at FROM messages WHERE workspace_id = ? AND channel = 'whatsapp' ORDER BY created_at DESC LIMIT 100",
            (ws,),
        ),
    }


@router.post("/connection")
def save_connection(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    token = (payload.get("access_token") or "").strip()
    phone_id = (payload.get("phone_number_id") or "").strip()
    values = {
        "phone_number_id": phone_id,
        "business_account_id": payload.get("business_account_id") or "",
        "status": "sandbox",
    }
    if token:
        values["access_token"] = token
    if token and phone_id:
        version = env("META_GRAPH_API_VERSION", "v25.0")
        try:
            response = requests.get(
                f"https://graph.facebook.com/{version}/{phone_id}",
                params={"access_token": token},
                timeout=15,
            )
            values["status"] = "connected" if response.ok else "sandbox"
            if not response.ok:
                upsert("whatsapp_connections", values, {"workspace_id": user["workspace_id"]})
                raise HTTPException(400, f"Meta rejected the connection: {response.text[:300]}")
        except requests.RequestException as error:
            upsert("whatsapp_connections", values, {"workspace_id": user["workspace_id"]})
            raise HTTPException(400, f"Could not reach Meta: {error}") from error
    upsert("whatsapp_connections", values, {"workspace_id": user["workspace_id"]})
    return {"ok": True, "status": values["status"]}


@router.post("/send")
def compose_send(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    phone = (payload.get("phone") or "").strip()
    body = (payload.get("body") or "").strip()
    if not phone or not body:
        raise HTTPException(400, "Recipient and message are required.")
    return {"ok": send_whatsapp(user["workspace_id"], phone, body, payload.get("lead_id") or "", account_id=payload.get("account_id") or "")}


@router.post("/inbound")
def record_inbound(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    return process_inbound(
        user["workspace_id"],
        "whatsapp",
        payload.get("sender") or "",
        payload.get("body") or "",
        payload.get("name") or "",
    )


whatsapp_router = router
