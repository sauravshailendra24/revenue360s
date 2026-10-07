from __future__ import annotations

import requests
from fastapi import APIRouter, Depends, HTTPException
from urllib.parse import urlparse

from auth import user_from_header
from db import PLATFORMS, encrypt_secret, execute, insert, json_text, now_iso, one, query,reveal,env,upsert
from engine import ai_write

router = APIRouter(tags=["social"])


@router.get("/api/social/accounts")
def list_accounts(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT id, platform, handle, status, created_at, updated_at FROM social_accounts WHERE workspace_id = ? ORDER BY created_at DESC",
        (user["workspace_id"],),
    )


@router.post("/api/social/accounts")
def connect_account(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    platform = (payload.get("platform") or "instagram").lower()
    handle = (payload.get("handle") or "").strip()
    token = (payload.get("access_token") or "").strip()
    if platform not in PLATFORMS:
        raise HTTPException(400, "Unknown platform.")
    if not handle:
        raise HTTPException(400, "Handle is required.")
    if not token:
        raise HTTPException(400, "An access token is required to connect this social account.")
    ws = user["workspace_id"]
    existing = one(
        "SELECT id FROM social_accounts WHERE workspace_id = ? AND platform = ? AND handle = ?",
        (ws, platform, handle),
    )
    if existing:
        execute(
            "UPDATE social_accounts SET status = 'connected', access_token = ?, updated_at = ? WHERE id = ?",
            (encrypt_secret(token) if token else "", now_iso(), existing["id"]),
        )
        return {"id": existing["id"], "updated": True}
    row_id = insert(
        "social_accounts",
        {"platform": platform, "handle": handle, "access_token": token, "status": "connected", "updated_at": now_iso()},
        ws,
    )
    return {"id": row_id, "updated": False}


@router.get("/api/schedule")
def list_schedule(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT * FROM scheduled_posts WHERE workspace_id = ? ORDER BY scheduled_at DESC LIMIT 40",
        (user["workspace_id"],),
    )


@router.post("/api/schedule")
def schedule_post(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    caption = (payload.get("caption") or "").strip()
    account_ids = payload.get("account_ids") or []
    networks = payload.get("networks") or []
    if account_ids:
        selected = []
        for account_id in account_ids:
            account = one(
                "SELECT id, platform, status, access_token FROM social_accounts WHERE id = ? AND workspace_id = ?",
                (account_id, user["workspace_id"]),
            )
            if not account:
                raise HTTPException(400, "A selected social account is unavailable in this workspace.")
            if account.get("status") != "connected" or not (reveal("social_accounts", account) or {}).get("access_token"):
                raise HTTPException(400, "Connect each selected social account before scheduling a post.")
            selected.append(account)
        networks = list(dict.fromkeys(account["platform"] for account in selected))
    if not caption or not networks:
        raise HTTPException(400, "Caption and at least one connected account are required.")
    media = str(payload.get("media") or "").strip()
    if len(media) > 2000:
        raise HTTPException(400, "The public video URL must be 2,000 characters or shorter.")
    selected_platforms = {account["platform"] for account in selected} if account_ids else set(networks)
    unsupported = selected_platforms - {"instagram", "threads", "linkedin"}
    if unsupported:
        raise HTTPException(400, "Scheduled publishing is currently supported for LinkedIn text, Instagram Reels, and Threads text posts.")
    if "instagram" in selected_platforms:
        parsed = urlparse(media)
        if parsed.scheme != "https" or not parsed.netloc:
            raise HTTPException(400, "Instagram Reel publishing needs a publicly reachable HTTPS video URL.")
    if "threads" in selected_platforms and media:
        raise HTTPException(400, "Threads scheduled publishing currently supports text posts. Remove the video URL or unselect Threads.")
    if "linkedin" in selected_platforms and media:
        raise HTTPException(400, "LinkedIn scheduled publishing currently supports text posts. Remove the video URL or unselect LinkedIn.")
    if media and "instagram" not in selected_platforms:
        raise HTTPException(400, "A video URL can only be scheduled as an Instagram Reel.")
    insert(
        "scheduled_posts",
        {
            "caption": caption,
            "networks": json_text(networks),
            "account_ids": json_text(account_ids),
            "media": media,
            "scheduled_at": payload.get("scheduled_at") or now_iso(),
            "status": "queued",
        },
        user["workspace_id"],
    )
    return {"ok": True}


@router.get("/api/social/platforms")
def platforms() -> list:
    return list(PLATFORMS)


@router.post("/api/social/draft")
def draft_social_post(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    platform = str(payload.get("platform") or "linkedin").strip().lower()
    brief = str(payload.get("brief") or "").strip()
    if platform != "linkedin":
        raise HTTPException(400, "LinkedIn post drafting is the only social writing workflow configured here.")
    if not brief:
        raise HTTPException(400, "Describe the idea or useful point for the post.")
    instruction = (
        "Draft a LinkedIn post from this brief: " + brief[:1800] +
        "\n\nUse a clear opening, one useful idea, concrete language, and a natural human voice. "
        "Do not invent results, customer stories, facts, or numbers. Avoid generic engagement bait and filler. "
        "Use at most three relevant hashtags. Return only the post text for the user to review."
    )
    draft = ai_write(user["workspace_id"], "linkedin", instruction, {}, [])
    if not draft:
        raise HTTPException(503, "No writing model is available. Configure the workspace AI provider, then try again.")
    return {"draft": draft, "requires_review": True}

social_router = router

router = APIRouter(prefix="/api/ads", tags=["ads"])

@router.get("")
def ads_page(account_id: str = "", user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    if account_id:
        row = one("SELECT * FROM meta_ad_accounts WHERE id = ? AND workspace_id = ?", (account_id, ws))
        if not row:
            raise HTTPException(404, "Ad account not found in this workspace.")
    else:
        row = one("SELECT * FROM meta_ad_accounts WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,))
    conn = reveal("meta_ad_accounts", row)
    events = query(
        "SELECT event_type, url, created_at FROM tracking_events WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 50",
        (ws,),
    )
    sequences = query("SELECT id, name FROM sequences WHERE workspace_id = ? ORDER BY created_at DESC", (ws,))
    return {
        "connection": {
            "id": (conn or {}).get("id", ""),
            "name": (conn or {}).get("name", ""),
            "ad_account_id": (conn or {}).get("ad_account_id", "") or env("META_AD_ACCOUNT_ID"),
            "page_id": (conn or {}).get("page_id", ""),
            "pixel_id": (conn or {}).get("pixel_id", ""),
            "default_sequence": (conn or {}).get("default_sequence", ""),
            "status": (conn or {}).get("status", "sandbox"),
            "has_token": bool((conn or {}).get("access_token") or env("META_ACCESS_TOKEN")),
        },
        "sequences": sequences,
        "accounts": query("SELECT id, name, ad_account_id, status FROM meta_ad_accounts WHERE workspace_id = ? ORDER BY created_at DESC", (ws,)),
        "events": events,
        "campaigns": [],
        "error": "",
    }

@router.post("/connection")
def save_connection(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    account_id = str(payload.get("id") or "").strip()
    ws = user["workspace_id"]
    if account_id and not one("SELECT id FROM meta_ad_accounts WHERE id = ? AND workspace_id = ?", (account_id, ws)):
        raise HTTPException(404, "Ad account not found in this workspace.")
    account = (payload.get("ad_account_id") or "").strip()
    values = {
        "name": (payload.get("name") or account).strip(),
        "ad_account_id": account,
        "page_id": payload.get("page_id") or "",
        "pixel_id": payload.get("pixel_id") or "",
        "default_sequence": payload.get("default_sequence") or "",
        "status": "connected" if account else "sandbox",
        "updated_at": now_iso(),
    }
    if payload.get("access_token"):
        values["access_token"] = payload["access_token"]
    if account_id:
        upsert("meta_ad_accounts", values, {"id": account_id, "workspace_id": ws})
    else:
        insert("meta_ad_accounts", values, ws)
    return {"ok": True}

@router.get("/campaigns")
def list_campaigns(account_id: str = "", user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    if account_id:
        row = one("SELECT * FROM meta_ad_accounts WHERE id = ? AND workspace_id = ?", (account_id, ws))
        if not row:
            raise HTTPException(404, "Ad account not found in this workspace.")
    else:
        row = one("SELECT * FROM meta_ad_accounts WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,))
    conn = reveal("meta_ad_accounts", row)
    token = env("META_ACCESS_TOKEN") or (conn or {}).get("access_token") or ""
    account = env("META_AD_ACCOUNT_ID") or (conn or {}).get("ad_account_id") or ""
    if not token or not account:
        return {"campaigns": [], "error": "Add an Ad Account ID and access token to load live campaigns."}
    if not account.startswith("act_"):
        account = f"act_{account}"
    version = env("META_GRAPH_API_VERSION", "v25.0")
    try:
        response = requests.get(
            f"https://graph.facebook.com/{version}/{account}/campaigns",
            params={
                "access_token": token,
                "fields": "id,name,status,objective,daily_budget,lifetime_budget,created_time",
                "limit": 25,
            },
            timeout=20,
        )
        if not response.ok:
            return {"campaigns": [], "error": response.text[:300]}
        return {"campaigns": response.json().get("data") or [], "error": ""}
    except requests.RequestException as error:
        return {"campaigns": [], "error": str(error)}


ads_router = router
