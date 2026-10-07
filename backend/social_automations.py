from __future__ import annotations

import json
import re

import requests
from fastapi import APIRouter, Depends, HTTPException

from auth import user_from_header
from db import env, execute, insert, new_id, now_iso, one, query, reveal

router = APIRouter(prefix="/api/social/comment-automations", tags=["social-automations"])

TEMPLATE_CHANNEL = {
    "instagram": "instagram_dm",
    "facebook": "messenger",
    "threads": "threads_reply",
}


def _settings(account: dict) -> dict:
    try:
        value = json.loads(account.get("settings") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _request(method: str, url: str, **kwargs) -> dict:
    try:
        response = requests.request(method, url, timeout=20, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError(f"Provider request failed ({type(error).__name__}).") from error
    if not response.ok:
        try:
            body = response.json()
            error = body.get("error") or body.get("message") or ""
            if isinstance(error, dict):
                error = error.get("message") or error.get("code") or ""
        except (ValueError, AttributeError):
            error = ""
        raise RuntimeError(f"Provider returned HTTP {response.status_code}" + (f": {str(error)[:250]}" if error else "."))
    try:
        return response.json()
    except ValueError:
        return {}


def _render_template(body: str, username: str, comment: str, post_id: str) -> str:
    values = {"username": username, "name": username, "comment": comment, "post_id": post_id}
    return re.sub(
        r"\{\{\s*(username|name|comment|post_id)\s*\}\}",
        lambda match: values[match.group(1).lower()],
        body,
        flags=re.IGNORECASE,
    ).strip()


def _send_reply(platform: str, account: dict, comment_id: str, text: str) -> str:
    token = str(account.get("access_token") or "").strip()
    account_id = str(account.get("handle") or "").strip()
    if not token or not account_id:
        raise RuntimeError("The connected account is missing its ID or access token.")
    settings = _settings(account)
    version = str(settings.get("api_version") or env("META_GRAPH_API_VERSION", "v25.0")).strip("/")

    if platform == "instagram":
        login_type = str(settings.get("login_type") or "facebook").lower()
        host = "graph.instagram.com" if login_type == "instagram" else "graph.facebook.com"
        result = _request(
            "POST",
            f"https://{host}/{version}/{account_id}/messages",
            headers={"Authorization": f"Bearer {token}"},
            json={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )
        return str(result.get("message_id") or result.get("id") or "")

    if platform == "facebook":
        result = _request(
            "POST",
            f"https://graph.facebook.com/{version}/{comment_id}/private_replies",
            headers={"Authorization": f"Bearer {token}"},
            data={"message": text},
        )
        return str(result.get("message_id") or result.get("id") or "")

    if platform == "threads":
        threads_version = str(settings.get("api_version") or "v1.0").strip("/")
        base = f"https://graph.threads.net/{threads_version}"
        created = _request(
            "POST",
            f"{base}/{account_id}/threads",
            data={"media_type": "TEXT", "text": text, "reply_to_id": comment_id, "access_token": token},
        )
        creation_id = str(created.get("id") or "")
        if not creation_id:
            raise RuntimeError("Threads did not return a reply creation ID.")
        published = _request(
            "POST",
            f"{base}/{account_id}/threads_publish",
            data={"creation_id": creation_id, "access_token": token},
        )
        return str(published.get("id") or "")

    raise RuntimeError("Comment replies are not supported for this platform.")


def process_comment(platform: str, entry_id: str, comment_id: str, text: str, post_id: str = "", username: str = "") -> bool:
    """Match one rule and send one response. Returns True when an event was accepted."""
    platform = str(platform or "").lower()
    comment_id = str(comment_id or "").strip()
    entry_id = str(entry_id or "").strip()
    if platform not in TEMPLATE_CHANNEL or not comment_id or not entry_id:
        return False

    accounts = query(
        "SELECT * FROM social_accounts WHERE platform = ? AND handle = ? AND status = 'connected'",
        (platform, entry_id),
    )
    if not accounts:
        return False
    account = accounts[0]
    rules = query(
        "SELECT * FROM social_comment_automations WHERE workspace_id = ? AND account_id = ? AND enabled = 1 ORDER BY (post_id <> '') DESC, created_at ASC",
        (account["workspace_id"], account["id"]),
    )
    text_folded = str(text or "").casefold()
    rule = next((item for item in rules
                 if (not item.get("post_id") or str(item["post_id"]) == str(post_id or ""))
                 and (not item.get("keyword") or str(item["keyword"]).casefold() in text_folded)), None)
    if not rule:
        return False

    template = one(
        "SELECT body FROM templates WHERE id = ? AND workspace_id = ? AND channel = ?",
        (rule["template_id"], account["workspace_id"], TEMPLATE_CHANNEL[platform]),
    )
    if not template or not str(template.get("body") or "").strip():
        return False

    event_id = new_id()
    timestamp = now_iso()
    execute(
        "INSERT IGNORE INTO social_comment_events "
        "(id, workspace_id, account_id, automation_id, platform, comment_id, post_id, comment_text, status, attempts, detail, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', 0, '', ?, ?)",
        (event_id, account["workspace_id"], account["id"], rule["id"], platform, comment_id, str(post_id or ""), str(text or "")[:2000], timestamp, timestamp),
    )
    inserted = one("SELECT id FROM social_comment_events WHERE workspace_id = ? AND platform = ? AND comment_id = ?", (account["workspace_id"], platform, comment_id))
    if not inserted or inserted["id"] != event_id:
        return False

    account = reveal("social_accounts", account) or {}
    _deliver_event(event_id, rule, account, template["body"], username)
    return True


def _deliver_event(event_id: str, rule: dict, account: dict, body: str, username: str = "") -> None:
    event = one("SELECT * FROM social_comment_events WHERE id = ?", (event_id,))
    if not event:
        return
    text = _render_template(body, username, event.get("comment_text") or "", event.get("post_id") or "")
    attempts = int(event.get("attempts") or 0) + 1
    try:
        response_id = _send_reply(event["platform"], account, event["comment_id"], text)
        execute(
            "UPDATE social_comment_events SET status = 'sent', response_id = ?, attempts = ?, detail = '', updated_at = ? WHERE id = ?",
            (response_id, attempts, now_iso(), event_id),
        )
        execute(
            "UPDATE social_comment_automations SET executions = executions + 1, updated_at = ? WHERE id = ? AND workspace_id = ?",
            (now_iso(), rule["id"], rule["workspace_id"]),
        )
    except RuntimeError as error:
        status = "failed" if attempts >= 5 else "pending"
        execute(
            "UPDATE social_comment_events SET status = ?, attempts = ?, detail = ?, updated_at = ? WHERE id = ?",
            (status, attempts, str(error)[:500], now_iso(), event_id),
        )


def handle_meta_change(object_type: str, entry_id: str, field: str, value: dict) -> bool:
    """Translate Meta Page, Instagram, and Threads comment/reply webhook values."""
    object_type = str(object_type or "").lower()
    field = str(field or "").lower()
    value = value if isinstance(value, dict) else {}
    if object_type == "instagram" and field in {"comments", "live_comments"}:
        media = value.get("media") if isinstance(value.get("media"), dict) else {}
        author = value.get("from") if isinstance(value.get("from"), dict) else {}
        if str(author.get("id") or "") == str(entry_id):
            return False
        return process_comment("instagram", entry_id, value.get("id") or value.get("comment_id"), value.get("text") or "", media.get("id") or value.get("media_id") or "", author.get("username") or value.get("username") or "")
    if object_type == "page" and field == "feed" and value.get("item") == "comment" and value.get("verb", "add") == "add":
        author = value.get("from") if isinstance(value.get("from"), dict) else {}
        if str(author.get("id") or "") == str(entry_id):
            return False
        return process_comment("facebook", entry_id, value.get("comment_id") or value.get("id"), value.get("message") or value.get("text") or "", value.get("post_id") or "", author.get("name") or value.get("username") or "")
    if object_type == "threads" and field == "replies":
        replied_to = value.get("replied_to") if isinstance(value.get("replied_to"), dict) else {}
        root_post = value.get("root_post") if isinstance(value.get("root_post"), dict) else {}
        author = value.get("from") if isinstance(value.get("from"), dict) else {}
        # Replies are user-generated comments; ignore a reply published by the connected account.
        if value.get("is_reply_owned_by_me") is True or str(author.get("id") or "") == str(entry_id):
            return False
        return process_comment("threads", entry_id, value.get("id") or value.get("reply_id"), value.get("text") or "", root_post.get("id") or value.get("root_post_id") or replied_to.get("id") or "", author.get("username") or value.get("username") or "")
    return False


@router.get("")
def list_automations(user: dict = Depends(user_from_header)) -> list[dict]:
    return query(
        "SELECT a.*, s.handle AS account_handle, s.name AS account_name, t.name AS template_name "
        "FROM social_comment_automations a "
        "JOIN social_accounts s ON s.id = a.account_id AND s.workspace_id = a.workspace_id "
        "JOIN templates t ON t.id = a.template_id AND t.workspace_id = a.workspace_id "
        "WHERE a.workspace_id = ? ORDER BY a.created_at DESC",
        (user["workspace_id"],),
    )


@router.post("")
def create_automation(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    account_id = str(payload.get("account_id") or "").strip()
    account = one("SELECT id, platform, status, access_token FROM social_accounts WHERE id = ? AND workspace_id = ?", (account_id, ws))
    if not account or account["platform"] not in TEMPLATE_CHANNEL or account.get("status") != "connected" or not (reveal("social_accounts", account) or {}).get("access_token"):
        raise HTTPException(400, "Choose a connected Instagram, Facebook Page, or Threads account.")
    template_id = str(payload.get("template_id") or "").strip()
    template = one("SELECT id FROM templates WHERE id = ? AND workspace_id = ? AND channel = ?", (template_id, ws, TEMPLATE_CHANNEL[account["platform"]]))
    if not template:
        raise HTTPException(400, f"Choose a template saved for {TEMPLATE_CHANNEL[account['platform']]}.")
    post_id = str(payload.get("post_id") or "").strip()
    automation_id = insert(
        "social_comment_automations",
        {
            "account_id": account_id,
            "platform": account["platform"],
            "name": str(payload.get("name") or "").strip() or "Comment reply",
            "post_id": post_id,
            "keyword": str(payload.get("keyword") or "").strip()[:255],
            "template_id": template_id,
            "enabled": 1,
            "updated_at": now_iso(),
        },
        ws,
    )
    return {"id": automation_id, "ok": True}


@router.post("/{automation_id}/enabled")
def set_automation_enabled(automation_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    enabled = 1 if payload.get("enabled") else 0
    existing = one("SELECT id FROM social_comment_automations WHERE id = ? AND workspace_id = ?", (automation_id, user["workspace_id"]))
    if not existing:
        raise HTTPException(404, "Comment automation not found.")
    execute("UPDATE social_comment_automations SET enabled = ?, updated_at = ? WHERE id = ? AND workspace_id = ?", (enabled, now_iso(), automation_id, user["workspace_id"]))
    return {"ok": True, "enabled": bool(enabled)}


@router.get("/events")
def list_events(user: dict = Depends(user_from_header)) -> list[dict]:
    return query(
        "SELECT e.id, e.platform, e.comment_id, e.post_id, e.comment_text, e.response_id, e.status, e.attempts, e.detail, e.created_at, a.name AS automation_name "
        "FROM social_comment_events e LEFT JOIN social_comment_automations a ON a.id = e.automation_id AND a.workspace_id = e.workspace_id "
        "WHERE e.workspace_id = ? ORDER BY e.created_at DESC LIMIT 100",
        (user["workspace_id"],),
    )


def retry_comment_events(limit: int = 20) -> int:
    pending = query(
        "SELECT e.*, a.template_id, t.body FROM social_comment_events e "
        "JOIN social_comment_automations a ON a.id = e.automation_id AND a.workspace_id = e.workspace_id "
        "JOIN templates t ON t.id = a.template_id AND t.workspace_id = a.workspace_id "
        "WHERE e.status = 'pending' AND e.attempts < 5 AND e.updated_at <= DATE_FORMAT(UTC_TIMESTAMP() - INTERVAL 20 SECOND, '%Y-%m-%dT%H:%i:%s+00:00') "
        "ORDER BY e.created_at ASC LIMIT ?",
        (limit,),
    )
    retried = 0
    for event in pending:
        account = one("SELECT * FROM social_accounts WHERE id = ? AND workspace_id = ?", (event["account_id"], event["workspace_id"]))
        if not account:
            execute("UPDATE social_comment_events SET status = 'failed', detail = 'Connected account is unavailable.', updated_at = ? WHERE id = ?", (now_iso(), event["id"]))
            continue
        _deliver_event(event["id"], {"id": event["automation_id"], "workspace_id": event["workspace_id"]}, reveal("social_accounts", account) or {}, event.get("body") or "")
        retried += 1
    return retried
