from __future__ import annotations
import json
from fastapi import APIRouter, Depends, HTTPException
from auth import user_from_header
from db import encrypt_secret, env, execute, insert, json_text, now_iso, one, public_url, query, reveal, secrets_locked, upsert
from engine import ai_write, parse_draft

router = APIRouter(prefix="/api/settings", tags=["settings"])
accounts_router = APIRouter(prefix="/api/accounts", tags=["accounts"])

ACCOUNT_TABLES = {
    "email": ("email_connections", "communication"),
    "whatsapp": ("whatsapp_connections", "communication"),
    "sms": ("sms_connections", "communication"),
    "instagram": ("social_accounts", "social"),
    "facebook": ("social_accounts", "social"),
    "threads": ("social_accounts", "social"),
    "tiktok": ("social_accounts", "social"),
    "youtube": ("social_accounts", "social"),
    "linkedin": ("social_accounts", "social"),
    "x": ("social_accounts", "social"),
    "meta_ads": ("meta_ad_accounts", "ads"),
    "google_ads": ("ad_network_accounts", "ads"),
    "linkedin_ads": ("ad_network_accounts", "ads"),
    "x_ads": ("ad_network_accounts", "ads"),
    "tiktok_ads": ("ad_network_accounts", "ads"),
    "microsoft_ads": ("ad_network_accounts", "ads"),
    "pinterest_ads": ("ad_network_accounts", "ads"),
    "snapchat_ads": ("ad_network_accounts", "ads"),
}


def _ad_credentials_ready(platform: str, credentials: dict, settings: dict) -> bool:
    if platform == "google_ads":
        return bool(
            credentials.get("refresh_token")
            and (credentials.get("client_id") or env("GOOGLE_ADS_CLIENT_ID"))
            and (credentials.get("client_secret") or env("GOOGLE_ADS_CLIENT_SECRET"))
        )
    if platform == "x_ads":
        return all(credentials.get(key) for key in ("consumer_key", "consumer_secret", "access_token", "access_token_secret"))
    if platform == "microsoft_ads":
        return bool(
            credentials.get("refresh_token")
            and (credentials.get("client_id") or env("MICROSOFT_ADS_CLIENT_ID"))
        ) and bool(
            credentials.get("developer_token") or env("MICROSOFT_ADS_DEVELOPER_TOKEN")
        ) and bool(settings.get("customer_id"))
    if platform in {"pinterest_ads", "snapchat_ads"}:
        return bool(credentials.get("access_token") or (
            credentials.get("refresh_token") and credentials.get("client_id") and credentials.get("client_secret")
        ))
    return bool(credentials.get("access_token"))


@accounts_router.get("")
def list_accounts(user: dict = Depends(user_from_header)) -> list:
    workspace_id = user["workspace_id"]
    rows = []
    for platform, (table, account_type) in ACCOUNT_TABLES.items():
        if table == "social_accounts":
            source_rows = query(
                "SELECT * FROM social_accounts WHERE workspace_id = ? AND platform = ? ORDER BY created_at DESC",
                (workspace_id, platform),
            )
        elif table == "ad_network_accounts":
            source_rows = query(
                "SELECT * FROM ad_network_accounts WHERE workspace_id = ? AND platform = ? ORDER BY created_at DESC",
                (workspace_id, platform),
            )
        else:
            source_rows = query(f"SELECT * FROM {table} WHERE workspace_id = ? ORDER BY created_at DESC", (workspace_id,))
        for source in source_rows:
            source = reveal(table, source) or {}
            identifier = {
                "email": source.get("email_address"),
                "whatsapp": source.get("phone_number_id"),
                "sms": source.get("sender_id"),
                "social": source.get("handle"),
                "ads": source.get("ad_account_id"),
            }[account_type]
            if table == "ad_network_accounts":
                identifier = source.get("advertiser_id")
            status = source.get("status") or "sandbox"
            if account_type == "social" and status == "connected" and not source.get("access_token"):
                status = "pending"
            if account_type == "ads" and status == "connected":
                status = "configured"
            settings = source.get("settings") or "{}"
            try:
                settings = json.loads(settings)
            except (TypeError, json.JSONDecodeError):
                settings = {}
            rows.append({
                "id": source["id"],
                "platform": source.get("platform") or platform,
                "account_type": account_type,
                "name": source.get("name") or identifier or platform.title(),
                "identifier": identifier or "",
                "status": status,
                "settings": settings,
                "has_credentials": bool(source.get("secret") or source.get("access_token") or source.get("credentials")),
                "created_at": source.get("created_at", ""),
                "updated_at": source.get("updated_at", ""),
            })
    return sorted(rows, key=lambda account: account["created_at"], reverse=True)


@accounts_router.post("")
def save_account(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    workspace_id = user["workspace_id"]
    platform = str(payload.get("platform") or "").strip().lower()
    if platform not in ACCOUNT_TABLES:
        raise HTTPException(400, "Choose a supported account platform.")
    table, _account_type = ACCOUNT_TABLES[platform]
    name = str(payload.get("name") or "").strip()
    identifier = str(payload.get("identifier") or "").strip()
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else {}
    if not identifier:
        raise HTTPException(400, "An account identifier is required.")
    account_id = str(payload.get("id") or "").strip()
    if account_id and not one(f"SELECT id FROM {table} WHERE id = ? AND workspace_id = ?", (account_id, workspace_id)):
        raise HTTPException(404, "Account not found in this workspace.")
    existing_account = reveal(table, one(f"SELECT * FROM {table} WHERE id = ? AND workspace_id = ?", (account_id, workspace_id))) if account_id else None

    values = {"name": name or identifier, "settings": json_text(settings), "updated_at": now_iso()}
    secret_fields = {}
    if platform == "email":
        values.update({"email_address": identifier, "smtp_host": settings.get("smtp_host") or "smtp.gmail.com", "smtp_port": str(settings.get("smtp_port") or "587"), "daily_cap": int(settings.get("daily_cap") or 500)})
        secret_fields["secret"] = credentials.get("secret")
    elif platform == "whatsapp":
        values.update({"phone_number_id": identifier, "business_account_id": settings.get("business_account_id") or ""})
        secret_fields["access_token"] = credentials.get("access_token")
    elif platform == "sms":
        values.update({"sender_id": identifier, "entity_id": settings.get("entity_id") or "", "template_id": settings.get("template_id") or "", "provider": settings.get("provider") or "msg91"})
        secret_fields["secret"] = credentials.get("secret")
    elif table == "social_accounts":
        values.update({"platform": platform, "handle": identifier})
        secret_fields["access_token"] = credentials.get("access_token")
    elif table == "ad_network_accounts":
        previous_credentials = {}
        try:
            previous_credentials = json.loads((existing_account or {}).get("credentials") or "{}")
        except (TypeError, json.JSONDecodeError):
            previous_credentials = {}
        if not isinstance(previous_credentials, dict):
            previous_credentials = {}
        previous_credentials.update({
            str(key): str(value).strip()
            for key, value in credentials.items()
            if value is not None and str(value).strip()
        })
        values.update({"platform": platform, "advertiser_id": identifier})
        if previous_credentials:
            values["credentials"] = json_text(previous_credentials)
        # Credentials have only been entered here. The provider is verified on
        # the first live campaign request, so don't claim a successful connection yet.
        values["status"] = "configured" if _ad_credentials_ready(platform, previous_credentials, settings) else "pending"
    else:
        values.update({"ad_account_id": identifier, "page_id": settings.get("page_id") or "", "pixel_id": settings.get("pixel_id") or "", "default_sequence": settings.get("default_sequence") or ""})
        secret_fields["access_token"] = credentials.get("access_token")

    values.update({key: value for key, value in secret_fields.items() if value})
    if table != "ad_network_accounts":
        values["status"] = "connected" if any(secret_fields.values()) else (existing_account or {}).get("status", "sandbox")
    if account_id:
        saved_id = upsert(table, values, {"id": account_id, "workspace_id": workspace_id})
    else:
        if table == "social_accounts":
            values["status"] = "connected" if secret_fields.get("access_token") else "pending"
        saved_id = insert(table, values, workspace_id)
    return {"id": saved_id, "ok": True}


@accounts_router.delete("/{account_id}")
def delete_account(account_id: str, user: dict = Depends(user_from_header)) -> dict:
    workspace_id = user["workspace_id"]
    for table in {entry[0] for entry in ACCOUNT_TABLES.values()}:
        existing = one(f"SELECT id FROM {table} WHERE id = ? AND workspace_id = ?", (account_id, workspace_id))
        if existing:
            execute(f"DELETE FROM {table} WHERE id = ? AND workspace_id = ?", (account_id, workspace_id))
            return {"ok": True}
    raise HTTPException(404, "Account not found in this workspace.")


@router.get("")
def get_settings(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    rows = {row["key"]: ("configured" if row["value"] else "") for row in query("SELECT `key`, value FROM settings_kv WHERE workspace_id = ?", (ws,))}
    email = reveal("email_connections", one("SELECT * FROM email_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    sms = reveal("sms_connections", one("SELECT * FROM sms_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    wa = reveal("whatsapp_connections", one("SELECT * FROM whatsapp_connections WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    ads = reveal("meta_ad_accounts", one("SELECT * FROM meta_ad_accounts WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)))
    return {
        "keys": rows,
        "encrypted": secrets_locked(),
        "email": {"email_address": (email or {}).get("email_address", ""), "status": (email or {}).get("status", "sandbox"), "has_secret": bool((email or {}).get("secret"))},
        "sms": {"sender_id": (sms or {}).get("sender_id", ""), "template_id": (sms or {}).get("template_id", ""), "has_secret": bool((sms or {}).get("secret"))},
        "whatsapp": {"phone_number_id": (wa or {}).get("phone_number_id", ""), "business_account_id": (wa or {}).get("business_account_id", ""), "status": (wa or {}).get("status", "sandbox")},
        "ads": {"ad_account_id": (ads or {}).get("ad_account_id", ""), "status": (ads or {}).get("status", "sandbox")},
        "webhook": f"{public_url()}/webhooks/meta",
        "verify_token": env("META_WEBHOOK_VERIFY_TOKEN", "revenue360s-verify"),
        "ai": {
            "status": "ready",
            "mode": "automatic",
            "router": {
                "classification": "cheap",
                "rewrite": "cheap",
                "caption": "cheap",
                "content_plan": "cheap",
                "workflow_plan": "reasoning",
                "complex_strategy": "reasoning",
                "vision": "vision",
            },
            "primary": "deepseek-flash",
        },
    }


@router.post("")
def save_settings(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    secrets_map = payload.get("secrets") or {}
    for key, value in secrets_map.items():
        if not str(value).strip():
            continue
        stored = encrypt_secret(str(value).strip()) if any(part in key for part in ("key", "token", "password", "secret")) else str(value).strip()
        existing = one("SELECT id FROM settings_kv WHERE workspace_id = ? AND `key` = ?", (ws, key))
        if existing:
            execute("UPDATE settings_kv SET value = ? WHERE id = ?", (stored, existing["id"]))
        else:
            insert("settings_kv", {"key": key, "value": stored}, ws)
    if payload.get("email"):
        email = payload["email"]
        values = {
            "email_address": email.get("email_address") or env("SMTP_USER", "stock360snoreply@gmail.com"),
            "smtp_host": email.get("smtp_host") or "smtp.gmail.com",
            "smtp_port": str(email.get("smtp_port") or "587"),
            "daily_cap": int(email.get("daily_cap") or 500),
            "status": "connected" if email.get("secret") or email.get("email_address") else "sandbox",
        }
        if email.get("secret"):
            values["secret"] = email["secret"]
        upsert("email_connections", values, {"workspace_id": ws})
    if payload.get("sms"):
        sms = payload["sms"]
        values = {
            "sender_id": sms.get("sender_id") or "",
            "template_id": sms.get("template_id") or "",
            "status": "connected" if sms.get("secret") else "sandbox",
        }
        if sms.get("secret"):
            values["secret"] = sms["secret"]
        upsert("sms_connections", values, {"workspace_id": ws})
    if payload.get("whatsapp"):
        wa = payload["whatsapp"]
        values = {
            "phone_number_id": wa.get("phone_number_id") or "",
            "business_account_id": wa.get("business_account_id") or "",
            "status": "connected" if wa.get("access_token") and wa.get("phone_number_id") else "sandbox",
        }
        if wa.get("access_token"):
            values["access_token"] = wa["access_token"]
        upsert("whatsapp_connections", values, {"workspace_id": ws})
    return {"ok": True}


@router.post("/draft")
def generate_draft(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    channel = payload.get("channel") or "whatsapp"
    lead = payload.get("lead") or {}
    thread = query_safe(ws, lead)
    draft = ai_write(ws, channel, payload.get("instruction") or "Write a short human follow-up.", lead, thread)
    subject, body = parse_draft(channel, draft)
    return {"subject": subject, "body": body or draft}


def query_safe(ws: str, lead: dict) -> list[dict]:
    from db import query

    if not lead.get("id"):
        return []
    return query(
        "SELECT direction, body FROM messages WHERE workspace_id = ? AND lead_id = ? ORDER BY created_at ASC LIMIT 12",
        (ws, lead.get("id")),
    )
