from __future__ import annotations
import hashlib
import hmac
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from auth import auth_router, company_router
from ad_platforms import router as ad_platforms_router
import learning
from db import env, env_flag, init_db, insert, one, public_url, require_encryption
from communication_channel import email_router, inbox_router, sms_router, templates_router, whatsapp_router
from engine import PIXEL, agent_router, audience_router, campaign_router, overview_router, process_inbound, sequences_router, workflows_router
from leads import router as leads_router
from settings import accounts_router, router as settings_router
from social_media import ads_router, social_router
from social_automations import handle_meta_change, router as social_automations_router
from search_intelligence import router as search_intelligence_router, ensure_tables as ensure_search_tables
from cloud import cloud_router, ensure_cloud_tables
from worker import refresh_router, ensure_refresh_tables
from billing import admin_router, billing_router, ensure_billing_tables
from public_site import router as public_site_router, ensure_public_tables

FRONTEND = ROOT / "frontend"

app = FastAPI(title="Revenue360s")

app.include_router(auth_router)
app.include_router(overview_router)
app.include_router(inbox_router)
app.include_router(leads_router)
app.include_router(sequences_router)
app.include_router(templates_router)
app.include_router(social_router)
app.include_router(social_automations_router)
app.include_router(workflows_router)
app.include_router(email_router)
app.include_router(sms_router)
app.include_router(whatsapp_router)
app.include_router(ads_router)
app.include_router(ad_platforms_router)
app.include_router(company_router)
app.include_router(settings_router)
app.include_router(accounts_router)
app.include_router(audience_router)
app.include_router(campaign_router)
app.include_router(agent_router)
app.include_router(search_intelligence_router)
app.include_router(cloud_router)
app.include_router(refresh_router)
app.include_router(billing_router)
app.include_router(admin_router)
app.include_router(public_site_router)

@app.on_event("startup")
def _start() -> None:
    require_encryption()
    init_db()
    learning.ensure_tables()
    ensure_search_tables()
    ensure_cloud_tables()
    ensure_refresh_tables()
    ensure_billing_tables()
    ensure_public_tables()

@app.get("/health")
def health() -> dict:
    return {"ok": True, "db": env("DB_NAME", "revenue360")}


@app.get("/webhooks/meta")
def meta_verify(request: Request):
    params = dict(request.query_params)
    if params.get("hub.mode") == "subscribe" and params.get("hub.verify_token") == env("META_WEBHOOK_VERIFY_TOKEN", "revenue360s-verify"):
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(403, "verify token mismatch")


def verify_meta_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    secret = env("META_APP_SECRET", "").strip()
    if not secret:
        # Fail closed: unsigned webhooks are accepted only when explicitly allowed (local dev).
        return env_flag("R360_ALLOW_UNSIGNED_WEBHOOKS")
    if not signature:
        return False
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature.strip())


@app.post("/webhooks/meta")
async def meta_inbound(request: Request) -> dict:
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not verify_meta_webhook_signature(raw_body, signature):
        raise HTTPException(403, "webhook signature mismatch")
    payload = await request.json()
    handled = 0
    object_type = str(payload.get("object") or "").lower()
    for entry in payload.get("entry") or []:
        entry_id = str(entry.get("id") or "")
        for change in entry.get("changes") or []:
            value = change.get("value") or {}
            if handle_meta_change(object_type, entry_id, str(change.get("field") or ""), value):
                handled += 1
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            connection = one("SELECT workspace_id FROM whatsapp_connections WHERE phone_number_id = ? LIMIT 1", (phone_number_id,)) if phone_number_id else None
            if not connection:
                continue
            for message in value.get("messages") or []:
                sender = str(message.get("from") or "")
                text = str(((message.get("text") or {}).get("body")) or "")
                if sender and text:
                    process_inbound(connection["workspace_id"], "whatsapp", sender, text)
                    handled += 1
    return {"ok": True, "handled": handled}


@app.get("/t/o/{message_id}.gif")
def track_open(message_id: str):
    message = one("SELECT * FROM messages WHERE id = ?", (message_id.replace(".gif", ""),))
    if message:
        exists = one("SELECT id FROM tracking_events WHERE message_id = ? AND event_type = 'open' LIMIT 1", (message["id"],))
        if not exists:
            insert(
                "tracking_events",
                {"message_id": message["id"], "lead_id": message.get("lead_id") or "", "event_type": "open", "url": ""},
                message["workspace_id"],
            )
    return Response(content=PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


@app.get("/t/c/{message_id}")
def track_click(message_id: str, u: str = ""):
    default = public_url()
    message = one("SELECT * FROM messages WHERE id = ?", (message_id,))
    target = (u or "").strip()
    # Only redirect to a link that was actually in the message we sent (no open redirect, external links work).
    if not message or not target.startswith(("http://", "https://")) or target not in (message.get("body") or ""):
        return RedirectResponse(default, status_code=302)
    insert(
        "tracking_events",
        {"message_id": message["id"], "lead_id": message.get("lead_id") or "", "event_type": "click", "url": target},
        message["workspace_id"],
    )
    try:
        learning.record_outcome(message["workspace_id"], message.get("lead_id") or "", "click")
    except Exception:
        pass
    return RedirectResponse(target, status_code=302)


app.mount("/static", StaticFiles(directory=FRONTEND), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(status_code=204)


@app.get("/admin", include_in_schema=False)
def admin_page():
    for path in (FRONTEND / "admin.html", APP_DIR / "admin.html"):
        if path.is_file():
            return FileResponse(path, headers={"Cache-Control": "no-store"})
    raise HTTPException(404, "Admin page is not deployed.")


@app.get("/billing/return", include_in_schema=False)
def billing_return(razorpay_payment_id: str = ""):
    reference = "".join(ch for ch in razorpay_payment_id if ch.isalnum() or ch == "_")[:80]
    shown = reference or "pending"
    return HTMLResponse(
        "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'><title>Payment received</title></head>"
        "<body style='font-family:sans-serif;max-width:520px;margin:48px auto'>"
        "<h1>Payment received</h1>"
        f"<p>Reference: {shown}</p>"
        "<p>This page does not grant access. Revenue360s activates the service after the payment is verified.</p>"
        "<p><a href='/'>Back to Revenue360s</a></p></body></html>",
        headers={"Cache-Control": "no-store"},
    )


PUBLIC_PAGES = {
    "product": "product.html",
    "pricing": "pricing.html",
    "solutions": "solutions.html",
    "demo": "demo.html",
    "contact": "contact.html",
    "privacy": "privacy.html",
    "terms": "terms.html",
    "marketing": "marketing.html",
}


def _page(name: str):
    path = FRONTEND / name
    if not path.is_file():
        raise HTTPException(404, "Page is not deployed.")
    return FileResponse(path, headers={"Cache-Control": "no-store"})


@app.get("/app", include_in_schema=False)
def app_page():
    return _page("app.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return RedirectResponse("/app", status_code=302)


@app.get("/signup", include_in_schema=False)
def signup_page():
    return RedirectResponse("/app?auth=signup", status_code=302)


@app.get("/robots.txt", include_in_schema=False)
def robots():
    return FileResponse(FRONTEND / "robots.txt")


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    return FileResponse(FRONTEND / "sitemap.xml", media_type="application/xml")


@app.get("/product", include_in_schema=False)
def product_page():
    return _page(PUBLIC_PAGES["product"])


@app.get("/pricing", include_in_schema=False)
def pricing_page():
    return _page(PUBLIC_PAGES["pricing"])


@app.get("/solutions", include_in_schema=False)
def solutions_page():
    return _page(PUBLIC_PAGES["solutions"])


@app.get("/demo", include_in_schema=False)
def demo_page():
    return _page(PUBLIC_PAGES["demo"])


@app.get("/contact", include_in_schema=False)
def contact_page():
    return _page(PUBLIC_PAGES["contact"])


@app.get("/privacy", include_in_schema=False)
def privacy_page():
    return _page(PUBLIC_PAGES["privacy"])


@app.get("/terms", include_in_schema=False)
def terms_page():
    return _page(PUBLIC_PAGES["terms"])


@app.get("/marketing", include_in_schema=False)
def marketing_page():
    return _page(PUBLIC_PAGES["marketing"])


@app.get("/")
def root():
    return _page("index.html")