from __future__ import annotations
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from auth import user_from_header
from db import db, env, env_flag, execute, insert, json_text, new_id, now_iso, one, query

GST_RATE = Decimal("0.18")
MONEY = Decimal("0.01")
TERM_DAYS = 30
DISCOUNT_LADDER = ((1, 0), (2, 10), (3, 12), (4, 15), (11, 20), (10_000, 30))
PLATFORM_FEES = {"small": 1999, "large": 3999, "pro_heavy": 7999}

CATALOG = [
    ("email_outreach", "Email Outreach", "Send sequences from the customer's own mailbox or a managed sender.", "outreach"),
    ("transactional_email", "Transactional Email", "Receipts, alerts, and one-to-one mail.", "outreach"),
    ("whatsapp", "WhatsApp Business", "WhatsApp Business API sending and inbox.", "outreach"),
    ("sms", "SMS", "DLT SMS, managed or bring-your-own provider.", "outreach"),
    ("seo", "SEO", "Search intelligence, crawl findings, and action plans.", "search"),
    ("analytics", "Website Analytics", "GA4 and site analytics inside the workspace.", "search"),
    ("gbp", "Google Business Profile", "Profile posts, reviews, and listing health.", "local"),
    ("maps", "Maps", "Maps presence, BYOP key or managed.", "local"),
    ("youtube", "YouTube Intelligence", "Channel and video intelligence.", "content"),
    ("indexing", "Indexing", "Index coverage and submission tracking.", "search"),
    ("ai", "AI", "Assistant, generation, and analysis credits.", "ai"),
    ("meta_social", "Meta / Social", "Connected social publishing, inbox, and comment automation.", "social"),
]

PLANS = [
    ("email_outreach", "starter", "byop", 799, "v1", 0),
    ("email_outreach", "growth", "byop", 1999, "v1", 0),
    ("email_outreach", "pro", "byop", 5499, "v1", 0),
    ("email_outreach", "starter", "managed", 1299, "v1", 0),
    ("email_outreach", "growth", "managed", 3299, "v1", 0),
    ("email_outreach", "pro", "managed", 9999, "v1", 0),
    ("transactional_email", "starter", "byop", 449, "v1", 0),
    ("transactional_email", "growth", "byop", 999, "v1", 0),
    ("transactional_email", "pro", "byop", 2499, "v1", 0),
    ("transactional_email", "starter", "managed", 749, "v1", 0),
    ("transactional_email", "growth", "managed", 1899, "v1", 0),
    ("transactional_email", "pro", "managed", 7999, "v1", 0),
    ("whatsapp", "starter", "standard", 1399, "v1", 0),
    ("whatsapp", "growth", "standard", 2799, "v1", 0),
    ("whatsapp", "pro", "standard", 5999, "v1", 0),
    ("sms", "starter", "byop", 399, "v1", 0),
    ("sms", "growth", "byop", 999, "v1", 0),
    ("sms", "pro", "byop", 2499, "v1", 0),
    ("sms", "starter", "managed", 949, "v1", 0),
    ("sms", "growth", "managed", 3299, "v1", 0),
    ("sms", "pro", "managed", 10499, "v1", 0),
    ("seo", "starter", "standard", 1999, "v3", 0),
    ("seo", "growth", "standard", 3999, "v3", 0),
    ("seo", "pro", "standard", 8999, "v3", 0),
    ("analytics", "starter", "standard", 499, "v1", 0),
    ("analytics", "growth", "standard", 1199, "v1", 0),
    ("analytics", "pro", "standard", 2999, "v1", 0),
    ("gbp", "starter", "standard", 2499, "v3", 0),
    ("gbp", "growth", "standard", 5999, "v3", 0),
    ("gbp", "pro", "standard", 12999, "v3", 0),
    ("maps", "starter", "byop", 599, "v1", 0),
    ("maps", "growth", "byop", 1499, "v1", 0),
    ("maps", "pro", "byop", 3499, "v1", 0),
    ("maps", "starter", "managed", 1999, "v1", 0),
    ("maps", "growth", "managed", 6999, "v1", 0),
    ("maps", "pro", "managed", 21999, "v1", 0),
    ("youtube", "starter", "standard", 799, "v1", 0),
    ("youtube", "growth", "standard", 1999, "v1", 0),
    ("youtube", "pro", "standard", 4999, "v1", 0),
    ("indexing", "starter", "standard", 349, "v1", 0),
    ("indexing", "growth", "standard", 799, "v1", 0),
    ("indexing", "pro", "standard", 1999, "v1", 0),
    ("ai", "starter", "managed", 1499, "v1", 1000),
    ("ai", "growth", "managed", 4999, "v1", 4000),
    ("ai", "pro", "managed", 14999, "v1", 12000),
    ("ai", "starter", "byok", 699, "v1", 0),
    ("ai", "growth", "byok", 1499, "v1", 0),
    ("ai", "pro", "byok", 3499, "v1", 0),
    ("meta_social", "starter", "standard", 1299, "v3", 0),
    ("meta_social", "growth", "standard", 2999, "v3", 0),
    ("meta_social", "pro", "standard", 6499, "v3", 0),
]

SERVICE_FEATURES = {
    "seo": ["seo_dashboard", "search_console", "seo_reports"],
    "gbp": ["gbp_dashboard", "gbp_reviews", "gbp_posts"],
    "analytics": ["analytics_dashboard", "ga4", "cloudflare"],
    "ai": ["ai_assistant", "ai_generation", "ai_analysis"],
    "meta_social": ["social_accounts", "social_publishing", "social_inbox", "social_comments"],
    "email_outreach": ["email_sequences", "email_campaigns"],
    "transactional_email": ["transactional_email"],
    "whatsapp": ["whatsapp_inbox", "whatsapp_send"],
    "sms": ["sms_send"],
    "maps": ["maps_dashboard"],
    "youtube": ["youtube_dashboard"],
    "indexing": ["indexing_dashboard"],
}

TIERS = {"starter", "growth", "pro"}
ENTITLEMENT_STATUSES = {"active", "expired", "suspended", "cancelled"}
PAYMENT_STATUSES = {"pending", "received", "verified", "failed", "refunded", "cancelled"}


def _money(value) -> Decimal:
    try:
        amount = Decimal(str(value))
    except Exception as error:
        raise HTTPException(400, "Amount must be numeric.") from error
    if not amount.is_finite():
        raise HTTPException(400, "Amount must be numeric.")
    return amount.quantize(MONEY, rounding=ROUND_HALF_UP)


def _paise(amount: Decimal) -> int:
    return int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _load(value, default):
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
        return loaded if loaded is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


def _period(when: str | None = None) -> str:
    stamp = when or now_iso()
    return stamp[:7]


def _plus_days(days: int, start: str | None = None) -> str:
    base = datetime.now(timezone.utc)
    if start:
        try:
            base = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
        except ValueError:
            base = datetime.now(timezone.utc)
    return (base + timedelta(days=int(days))).replace(microsecond=0).isoformat()


def ensure_billing_tables() -> None:
    statements = [
        "CREATE TABLE IF NOT EXISTS service_catalog ("
        "service_code VARCHAR(40) PRIMARY KEY, name VARCHAR(120) NOT NULL, description VARCHAR(500) NOT NULL DEFAULT '', "
        "category VARCHAR(40) NOT NULL DEFAULT '', active TINYINT NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL)",
        "CREATE TABLE IF NOT EXISTS service_plans ("
        "id VARCHAR(36) PRIMARY KEY, service_code VARCHAR(40) NOT NULL, tier VARCHAR(20) NOT NULL, mode VARCHAR(20) NOT NULL DEFAULT 'standard', "
        "country VARCHAR(8) NOT NULL DEFAULT 'IN', currency VARCHAR(8) NOT NULL DEFAULT 'INR', price DECIMAL(12,2) NOT NULL, "
        "price_version VARCHAR(12) NOT NULL DEFAULT 'v1', quota_units INT NOT NULL DEFAULT 0, "
        "razorpay_payment_link_id VARCHAR(80) NOT NULL DEFAULT '', razorpay_payment_link_url VARCHAR(500) NOT NULL DEFAULT '', "
        "active TINYINT NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL DEFAULT '', updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_service_plan (service_code, tier, mode, country))",
        "CREATE TABLE IF NOT EXISTS payment_records ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, user_id VARCHAR(36) NOT NULL DEFAULT '', "
        "customer_email VARCHAR(255) NOT NULL DEFAULT '', service_code VARCHAR(40) NOT NULL DEFAULT '', tier VARCHAR(20) NOT NULL DEFAULT '', "
        "mode VARCHAR(20) NOT NULL DEFAULT '', amount DECIMAL(12,2) NOT NULL DEFAULT 0, discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0, "
        "platform_fee DECIMAL(12,2) NOT NULL DEFAULT 0, currency VARCHAR(8) NOT NULL DEFAULT 'INR', gst_amount DECIMAL(12,2) NOT NULL DEFAULT 0, "
        "total_amount DECIMAL(12,2) NOT NULL DEFAULT 0, payment_method VARCHAR(40) NOT NULL DEFAULT '', "
        "razorpay_payment_link_id VARCHAR(80) NOT NULL DEFAULT '', razorpay_payment_link_url VARCHAR(500) NOT NULL DEFAULT '', "
        "razorpay_payment_id VARCHAR(80) NOT NULL DEFAULT '', razorpay_order_id VARCHAR(80) NOT NULL DEFAULT '', "
        "status VARCHAR(20) NOT NULL DEFAULT 'pending', payment_date VARCHAR(40) NOT NULL DEFAULT '', source VARCHAR(20) NOT NULL DEFAULT 'manual', "
        "notes LONGTEXT, quote_json LONGTEXT, verified_by VARCHAR(36) NOT NULL DEFAULT '', verified_at VARCHAR(40) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_pay_ws (workspace_id, created_at), INDEX idx_pay_email (customer_email), INDEX idx_pay_status (status))",
        "CREATE TABLE IF NOT EXISTS payment_items ("
        "id VARCHAR(36) PRIMARY KEY, payment_id VARCHAR(36) NOT NULL, workspace_id VARCHAR(36) NOT NULL, service_code VARCHAR(40) NOT NULL, tier VARCHAR(20) NOT NULL, "
        "mode VARCHAR(20) NOT NULL DEFAULT 'standard', list_price DECIMAL(12,2) NOT NULL, discount_amount DECIMAL(12,2) NOT NULL DEFAULT 0, "
        "net_amount DECIMAL(12,2) NOT NULL, INDEX idx_pay_items (payment_id))",
        "CREATE TABLE IF NOT EXISTS workspace_entitlements ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, service_code VARCHAR(40) NOT NULL, tier VARCHAR(20) NOT NULL, "
        "mode VARCHAR(20) NOT NULL DEFAULT 'standard', status VARCHAR(20) NOT NULL DEFAULT 'active', starts_at VARCHAR(40) NOT NULL, "
        "expires_at VARCHAR(40) NOT NULL, payment_id VARCHAR(36) NOT NULL DEFAULT '', source VARCHAR(20) NOT NULL DEFAULT 'admin', "
        "manual_reason VARCHAR(500) NOT NULL DEFAULT '', quota_units INT NOT NULL DEFAULT 0, created_by VARCHAR(36) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_ent_ws (workspace_id, service_code, status))",
        "CREATE TABLE IF NOT EXISTS entitlement_usage ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, service_code VARCHAR(40) NOT NULL, period VARCHAR(7) NOT NULL, "
        "used_units INT NOT NULL DEFAULT 0, updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_usage (workspace_id, service_code, period))",
        "CREATE TABLE IF NOT EXISTS admin_audit_log ("
        "id VARCHAR(36) PRIMARY KEY, admin_user_id VARCHAR(36) NOT NULL DEFAULT '', action VARCHAR(60) NOT NULL, "
        "target_type VARCHAR(40) NOT NULL DEFAULT '', target_id VARCHAR(36) NOT NULL DEFAULT '', before_data LONGTEXT, after_data LONGTEXT, "
        "ip_address VARCHAR(64) NOT NULL DEFAULT '', created_at VARCHAR(40) NOT NULL, INDEX idx_admin_audit (created_at))",
    ]
    for statement in statements:
        execute(statement)
    _ensure_user_role()
    _seed_catalog()
    _promote_env_admin()


def _ensure_user_role() -> None:
    row = one("SHOW COLUMNS FROM users LIKE 'role'")
    if not row:
        execute("ALTER TABLE users ADD COLUMN role VARCHAR(20) NOT NULL DEFAULT 'user'")


def _seed_catalog() -> None:
    for code, name, description, category in CATALOG:
        if not one("SELECT service_code FROM service_catalog WHERE service_code = ?", (code,)):
            execute(
                "INSERT INTO service_catalog (service_code, name, description, category, active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                (code, name, description, category, now_iso()),
            )
    for code, tier, mode, price, version, quota in PLANS:
        if one(
            "SELECT id FROM service_plans WHERE service_code = ? AND tier = ? AND mode = ? AND country = 'IN'",
            (code, tier, mode),
        ):
            continue
        execute(
            "INSERT INTO service_plans (id, service_code, tier, mode, country, currency, price, price_version, quota_units, active, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'IN', 'INR', ?, ?, ?, ?, ?, ?)",
            (new_id(), code, tier, mode, price, version, quota, 1 if version == "v3" else 0, now_iso(), now_iso()),
        )
    # Component-table drafts must not be sold. Admin confirmation sets price_version=confirmed.
    execute(
        "UPDATE service_plans SET active = 0, updated_at = ? WHERE price_version NOT IN ('v3', 'confirmed') AND active = 1",
        (now_iso(),),
    )


def _promote_env_admin() -> None:
    email = env("R360_ADMIN_EMAIL", "").strip().lower()
    if not email:
        return
    execute("UPDATE users SET role = 'admin' WHERE email = ? AND role <> 'admin'", (email,))


def discount_percent(tab_count: int) -> int:
    count = max(int(tab_count or 0), 0)
    for ceiling, percent in DISCOUNT_LADDER:
        if count <= ceiling:
            return percent
    return 30


def platform_fee(items: list[dict]) -> Decimal:
    count = len(items)
    pro_count = sum(1 for item in items if str(item.get("tier")) == "pro")
    if pro_count >= 2 or (pro_count >= 1 and count >= 5):
        return _money(PLATFORM_FEES["pro_heavy"])
    if count >= 5:
        return _money(PLATFORM_FEES["large"])
    if count >= 1:
        return _money(PLATFORM_FEES["small"])
    return _money(0)


def build_quote(lines: list[dict]) -> dict:
    """Pure pricing. lines are already resolved plan rows plus the requested tier/mode."""
    if not lines:
        raise HTTPException(400, "Choose at least one service.")
    if len(lines) > 12:
        raise HTTPException(400, "A cart can include at most 12 services.")
    seen = set()
    resolved = []
    for line in lines:
        code = str(line.get("service_code") or "")
        if code in seen:
            raise HTTPException(400, "Each service can appear once in a cart.")
        seen.add(code)
        price = _money(line["price"])
        resolved.append({
            "service_code": code,
            "name": line.get("name") or code,
            "tier": line["tier"],
            "mode": line.get("mode") or "standard",
            "list_price": price,
            "price_version": line.get("price_version") or "",
            "quota_units": int(line.get("quota_units") or 0),
            "razorpay_payment_link_id": line.get("razorpay_payment_link_id") or "",
            "razorpay_payment_link_url": line.get("razorpay_payment_link_url") or "",
        })
    percent = discount_percent(len(resolved))
    rate = Decimal(percent) / Decimal(100)
    subtotal = sum((item["list_price"] for item in resolved), Decimal("0"))
    discount = _money(subtotal * rate)
    # Spread the discount across lines so each entitlement has a net price. Last line absorbs rounding.
    remaining = discount
    for index, item in enumerate(resolved):
        if index == len(resolved) - 1:
            line_discount = remaining
        else:
            line_discount = _money(item["list_price"] * rate)
            remaining -= line_discount
        item["discount_amount"] = line_discount
        item["net_amount"] = _money(item["list_price"] - line_discount)
    fee = platform_fee(resolved)
    taxable = _money(subtotal - discount + fee)
    gst = _money(taxable * GST_RATE)
    total = _money(taxable + gst)
    return {
        "currency": "INR",
        "tab_count": len(resolved),
        "discount_percent": percent,
        "subtotal": subtotal,
        "discount_amount": discount,
        "platform_fee": fee,
        "taxable_amount": taxable,
        "gst_rate": GST_RATE,
        "gst_amount": gst,
        "total_amount": total,
        "items": resolved,
        "note": "Bundle discount applies to tab prices only. Platform fee is not discounted. GST is 18% on discounted tabs plus fee.",
    }


def _public_quote(quote: dict) -> dict:
    out = dict(quote)
    for key in ("subtotal", "discount_amount", "platform_fee", "taxable_amount", "gst_amount", "total_amount"):
        out[key] = float(quote[key])
    out["gst_rate"] = float(quote["gst_rate"])
    out["items"] = []
    for item in quote["items"]:
        copied = dict(item)
        for key in ("list_price", "discount_amount", "net_amount"):
            copied[key] = float(item[key])
        out["items"].append(copied)
    return out


def _plan(service_code: str, tier: str, mode: str) -> dict:
    row = one(
        "SELECT p.*, c.name FROM service_plans p JOIN service_catalog c ON c.service_code = p.service_code "
        "WHERE p.service_code = ? AND p.tier = ? AND p.mode = ? AND p.country = 'IN' AND p.active = 1 AND c.active = 1",
        (service_code, tier, mode),
    )
    if not row:
        raise HTTPException(400, f"No active India plan for {service_code} / {tier} / {mode}.")
    return row


def _normalize_items(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "items must be a non-empty list of {service_code, tier, mode}.")
    lines = []
    for item in raw:
        if not isinstance(item, dict):
            raise HTTPException(400, "Each cart item must be an object.")
        code = str(item.get("service_code") or "").strip()
        tier = str(item.get("tier") or "").strip().lower()
        mode = str(item.get("mode") or "standard").strip().lower()
        if tier not in TIERS:
            raise HTTPException(400, "Tier must be starter, growth, or pro.")
        lines.append(_plan(code, tier, mode))
    return lines


def require_admin(user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT id, email, role FROM users WHERE id = ?", (user["user_id"],))
    allowed = env("R360_ADMIN_EMAIL", "").strip().lower()
    email = str((row or {}).get("email") or "").lower()
    if not row or row.get("role") != "admin":
        raise HTTPException(403, "Admin access required.")
    if allowed and email != allowed:
        raise HTTPException(403, "Admin access required.")
    return {**user, "role": "admin", "email": email}


def _client_ip(request: Request | None) -> str:
    if request is None:
        return ""
    forwarded = request.headers.get("x-forwarded-for") or ""
    return (forwarded.split(",")[0] if forwarded else request.client.host if request.client else "")[:64]


def audit_admin(admin: dict, action: str, target_type: str, target_id: str, before, after, request: Request | None = None) -> None:
    execute(
        "INSERT INTO admin_audit_log (id, admin_user_id, action, target_type, target_id, before_data, after_data, ip_address, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), admin.get("user_id") or "", action, target_type, target_id or "", json_text(before or {}), json_text(after or {}), _client_ip(request), now_iso()),
    )


def _active_entitlement(workspace_id: str, service_code: str) -> dict | None:
    row = one(
        "SELECT * FROM workspace_entitlements WHERE workspace_id = ? AND service_code = ? AND status = 'active' ORDER BY expires_at DESC LIMIT 1",
        (workspace_id, service_code),
    )
    if not row:
        return None
    if str(row.get("expires_at") or "") < now_iso():
        execute(
            "UPDATE workspace_entitlements SET status = 'expired', updated_at = ? WHERE id = ? AND status = 'active'",
            (now_iso(), row["id"]),
        )
        return None
    return row


def require_service(user: dict, service_code: str) -> dict:
    code = str(service_code or "").strip()
    row = _active_entitlement(user["workspace_id"], code)
    if not row:
        raise HTTPException(402, f"{code} is not active for this workspace.")
    return row


def require_feature(user: dict, feature: str) -> dict:
    for service_code, features in SERVICE_FEATURES.items():
        if feature in features:
            return require_service(user, service_code)
    raise HTTPException(400, "Unknown feature.")


def consume_quota(workspace_id: str, service_code: str, units: int) -> dict:
    if units <= 0:
        raise HTTPException(400, "Quota units must be greater than zero.")
    entitlement = _active_entitlement(workspace_id, service_code)
    if not entitlement:
        raise HTTPException(402, f"{service_code} is not active for this workspace.")
    included = int(entitlement.get("quota_units") or 0)
    if included <= 0:
        return {"ok": True, "limited": False, "used_units": 0, "included_units": 0}
    period = _period()
    stamp = now_iso()
    with db() as connection:
        with connection.cursor() as cur:
            cur.execute(
                "UPDATE entitlement_usage SET used_units = used_units + %s, updated_at = %s "
                "WHERE workspace_id = %s AND service_code = %s AND period = %s AND used_units + %s <= %s",
                (units, stamp, workspace_id, service_code, period, units, included),
            )
            if cur.rowcount == 1:
                cur.execute(
                    "SELECT used_units FROM entitlement_usage WHERE workspace_id = %s AND service_code = %s AND period = %s",
                    (workspace_id, service_code, period),
                )
                used = int((cur.fetchone() or {}).get("used_units") or 0)
                return {"ok": True, "limited": True, "used_units": used, "included_units": included}
            cur.execute(
                "SELECT used_units FROM entitlement_usage WHERE workspace_id = %s AND service_code = %s AND period = %s",
                (workspace_id, service_code, period),
            )
            existing = cur.fetchone()
            if existing:
                raise HTTPException(402, f"{service_code} quota exhausted for {period}. Upgrade or buy overage.")
            try:
                cur.execute(
                    "INSERT INTO entitlement_usage (id, workspace_id, service_code, period, used_units, updated_at) VALUES (%s, %s, %s, %s, %s, %s)",
                    (new_id(), workspace_id, service_code, period, units, stamp),
                )
            except Exception:
                connection.rollback()
                raise HTTPException(409, f"{service_code} quota is being updated. Retry.") from None
    return {"ok": True, "limited": True, "used_units": units, "included_units": included}


def _activate_on(cur, workspace_id: str, items: list[dict], payment_id: str, actor: str) -> list[dict]:
    activated = []
    start = now_iso()
    for item in items:
        cur.execute(
            "SELECT quota_units FROM service_plans WHERE service_code = %s AND tier = %s AND mode = %s AND country = 'IN' LIMIT 1",
            (item["service_code"], item["tier"], item["mode"]),
        )
        plan = cur.fetchone() or {}
        quota = int(plan.get("quota_units") or 0)
        cur.execute(
            "SELECT id, status, expires_at, payment_id FROM workspace_entitlements WHERE workspace_id = %s AND service_code = %s ORDER BY expires_at DESC LIMIT 1",
            (workspace_id, item["service_code"]),
        )
        existing = cur.fetchone()
        base = start
        if existing and existing.get("status") == "active" and str(existing.get("expires_at") or "") > start:
            base = existing["expires_at"]
        expires = _plus_days(TERM_DAYS, None if base == start else base)
        if existing and existing.get("status") in {"active", "expired", "suspended"}:
            cur.execute(
                "UPDATE workspace_entitlements SET tier = %s, mode = %s, status = 'active', starts_at = %s, expires_at = %s, payment_id = %s, "
                "source = 'payment', quota_units = %s, updated_at = %s WHERE id = %s",
                (item["tier"], item.get("mode") or "standard", start, expires, payment_id or existing.get("payment_id") or "", quota, start, existing["id"]),
            )
            entitlement_id = existing["id"]
        else:
            entitlement_id = new_id()
            cur.execute(
                "INSERT INTO workspace_entitlements (id, workspace_id, service_code, tier, mode, status, starts_at, expires_at, payment_id, source, "
                "manual_reason, quota_units, created_by, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, 'active', %s, %s, %s, 'payment', '', %s, %s, %s, %s)",
                (entitlement_id, workspace_id, item["service_code"], item["tier"], item.get("mode") or "standard", start, expires, payment_id, quota, actor, start, start),
            )
        activated.append({"entitlement_id": entitlement_id, "service_code": item["service_code"], "tier": item["tier"], "expires_at": expires})
    return activated


def _upsert_entitlement(workspace_id: str, item: dict, payment_id: str, source: str, reason: str, actor: str, days: int = TERM_DAYS) -> str:
    existing = one(
        "SELECT * FROM workspace_entitlements WHERE workspace_id = ? AND service_code = ? ORDER BY expires_at DESC LIMIT 1",
        (workspace_id, item["service_code"]),
    )
    start = now_iso()
    base = existing.get("expires_at") if existing and str(existing.get("expires_at") or "") > start and existing.get("status") == "active" else start
    expires = _plus_days(days, base if base != start else None)
    quota = int(item.get("quota_units") or 0)
    if existing and existing.get("status") in {"active", "expired", "suspended"}:
        execute(
            "UPDATE workspace_entitlements SET tier = ?, mode = ?, status = 'active', starts_at = ?, expires_at = ?, payment_id = ?, source = ?, "
            "manual_reason = ?, quota_units = ?, updated_at = ? WHERE id = ?",
            (item["tier"], item.get("mode") or "standard", start, expires, payment_id or existing.get("payment_id") or "", source, reason[:500], quota, now_iso(), existing["id"]),
        )
        return existing["id"]
    return insert(
        "workspace_entitlements",
        {
            "workspace_id": workspace_id,
            "service_code": item["service_code"],
            "tier": item["tier"],
            "mode": item.get("mode") or "standard",
            "status": "active",
            "starts_at": start,
            "expires_at": expires,
            "payment_id": payment_id,
            "source": source,
            "manual_reason": reason[:500],
            "quota_units": quota,
            "created_by": actor,
            "updated_at": now_iso(),
        },
        workspace_id,
    )


def _payment_public(row: dict, items: list[dict] | None = None) -> dict:
    if not row:
        return {}
    out = dict(row)
    for key in ("amount", "discount_amount", "platform_fee", "gst_amount", "total_amount"):
        if key in out and out[key] is not None:
            out[key] = float(out[key])
    out["quote"] = _load(out.pop("quote_json", None), {})
    out["items"] = items if items is not None else []
    return out


def _create_razorpay_link(payment: dict, quote: dict, email: str, name: str) -> dict:
    key_id = env("RAZORPAY_KEY_ID", "")
    key_secret = env("RAZORPAY_KEY_SECRET", "")
    if not key_id or not key_secret:
        return {}
    payload = {
        "amount": _paise(_money(quote["total_amount"])),
        "currency": "INR",
        "description": "Revenue360s " + ", ".join(f"{item['service_code']} {item['tier']}" for item in quote["items"])[:240],
        "reference_id": payment["id"][:40],
        "customer": {"email": email, "name": name or email},
        "notify": {"email": True, "sms": False},
        "notes": {"payment_record_id": payment["id"], "workspace_id": payment["workspace_id"]},
        "reminder_enable": True,
    }
    callback = env("R360_PUBLIC_URL", "").rstrip("/")
    if callback:
        payload["callback_url"] = callback + "/billing/return"
        payload["callback_method"] = "get"
    try:
        response = requests.post("https://api.razorpay.com/v1/payment_links", auth=(key_id, key_secret), json=payload, timeout=25)
    except requests.RequestException as error:
        raise HTTPException(502, f"Razorpay payment link request failed: {type(error).__name__}") from error
    if not response.ok:
        raise HTTPException(502, f"Razorpay returned HTTP {response.status_code}: {response.text[:180]}")
    body = response.json()
    return {"id": body.get("id") or "", "url": body.get("short_url") or body.get("url") or ""}


billing_router = APIRouter(prefix="/api/billing", tags=["billing"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin"])


@billing_router.get("/catalog")
def catalog(user: dict = Depends(user_from_header)) -> dict:
    del user
    ensure_billing_tables()
    services = query("SELECT service_code, name, description, category FROM service_catalog WHERE active = 1 ORDER BY category, name")
    plans = query(
        "SELECT service_code, tier, mode, country, currency, price, price_version, quota_units, razorpay_payment_link_id, razorpay_payment_link_url "
        "FROM service_plans WHERE active = 1 AND country = 'IN' ORDER BY service_code, mode, FIELD(tier, 'starter', 'growth', 'pro')"
    )
    for plan in plans:
        plan["price"] = float(plan["price"])
        plan["payment_link_ready"] = bool(plan.get("razorpay_payment_link_url"))
    return {
        "services": services,
        "plans": plans,
        "discount_ladder": [{"max_tabs": ceiling, "percent": percent} for ceiling, percent in DISCOUNT_LADDER],
        "platform_fees": PLATFORM_FEES,
        "gst_rate": 0.18,
        "features": SERVICE_FEATURES,
    }


@billing_router.post("/quote")
def quote(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    del user
    ensure_billing_tables()
    return _public_quote(build_quote(_normalize_items(payload.get("items"))))


@billing_router.post("/checkout")
def checkout(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ensure_billing_tables()
    quote_raw = build_quote(_normalize_items(payload.get("items")))
    payment_id = new_id()
    single = quote_raw["items"][0] if len(quote_raw["items"]) == 1 else None
    link_id = single.get("razorpay_payment_link_id") if single else ""
    link_url = single.get("razorpay_payment_link_url") if single else ""
    if single and not link_url:
        raise HTTPException(400, "This plan has no Razorpay Payment Link. Attach one in admin before selling it.")
    email = user.get("email") or ""
    record = {
        "id": payment_id,
        "workspace_id": user["workspace_id"],
        "user_id": user["user_id"],
        "customer_email": email,
        "service_code": single["service_code"] if single else "bundle",
        "tier": single["tier"] if single else "",
        "mode": single["mode"] if single else "",
        "amount": quote_raw["subtotal"],
        "discount_amount": quote_raw["discount_amount"],
        "platform_fee": quote_raw["platform_fee"],
        "currency": "INR",
        "gst_amount": quote_raw["gst_amount"],
        "total_amount": quote_raw["total_amount"],
        "payment_method": "razorpay_link",
        "razorpay_payment_link_id": link_id,
        "razorpay_payment_link_url": link_url,
        "status": "pending",
        "source": "razorpay_link",
        "notes": str(payload.get("notes") or "")[:1000],
        "quote_json": json_text(_public_quote(quote_raw)),
        "updated_at": now_iso(),
    }
    insert("payment_records", record, user["workspace_id"])
    for item in quote_raw["items"]:
        insert(
            "payment_items",
            {
                "payment_id": payment_id,
                "service_code": item["service_code"],
                "tier": item["tier"],
                "mode": item["mode"],
                "list_price": item["list_price"],
                "discount_amount": item["discount_amount"],
                "net_amount": item["net_amount"],
            },
            user["workspace_id"],
        )
    if not single:
        return {
            "id": payment_id,
            "status": "pending",
            "quote": _public_quote(quote_raw),
            "razorpay_payment_link_id": "",
            "razorpay_payment_link_url": "",
            "activated": False,
            "detail": "Bundle recorded as pending. No Razorpay link was created. Collect it manually, then verify and activate.",
        }
    return {
        "id": payment_id,
        "status": "pending",
        "quote": _public_quote(quote_raw),
        "razorpay_payment_link_id": link_id,
        "razorpay_payment_link_url": link_url,
        "activated": False,
        "detail": "Use the Payment Link stored on this plan. Access starts only after verify and activate.",
    }


@billing_router.get("/payments")
def my_payments(user: dict = Depends(user_from_header)) -> list:
    ensure_billing_tables()
    rows = query(
        "SELECT id, service_code, tier, mode, total_amount, currency, status, source, payment_date, created_at FROM payment_records "
        "WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 50",
        (user["workspace_id"],),
    )
    for row in rows:
        row["total_amount"] = float(row["total_amount"])
    return rows


@billing_router.get("/subscriptions")
def my_subscriptions(user: dict = Depends(user_from_header)) -> list:
    ensure_billing_tables()
    rows = query(
        "SELECT service_code, tier, mode, status, starts_at, expires_at, quota_units, source FROM workspace_entitlements "
        "WHERE workspace_id = ? ORDER BY service_code",
        (user["workspace_id"],),
    )
    period = _period()
    usage = {
        row["service_code"]: int(row["used_units"])
        for row in query(
            "SELECT service_code, used_units FROM entitlement_usage WHERE workspace_id = ? AND period = ?",
            (user["workspace_id"], period),
        )
    }
    for row in rows:
        row["used_units"] = usage.get(row["service_code"], 0)
        row["active"] = row["status"] == "active" and str(row["expires_at"]) >= now_iso()
    return rows


@admin_router.get("/overview")
def admin_overview(admin: dict = Depends(require_admin)) -> dict:
    del admin
    ensure_billing_tables()
    customers = one("SELECT COUNT(*) AS n FROM users")["n"]
    active = one("SELECT COUNT(*) AS n FROM workspace_entitlements WHERE status = 'active' AND expires_at >= ?", (now_iso(),))["n"]
    pending = one("SELECT COUNT(*) AS n FROM payment_records WHERE status = 'pending'")["n"]
    failed = one("SELECT COUNT(*) AS n FROM payment_records WHERE status = 'failed'")["n"]
    month = now_iso()[:7]
    revenue = one(
        "SELECT COALESCE(SUM(total_amount), 0) AS n FROM payment_records WHERE status IN ('received', 'verified') AND created_at LIKE ?",
        (month + "%",),
    )["n"]
    return {
        "customers": customers,
        "active_subscriptions": active,
        "pending_payments": pending,
        "failed_payments": failed,
        "month_revenue": float(revenue or 0),
        "currency": "INR",
        "month": month,
    }


@admin_router.get("/payments")
def admin_payments(q: str = "", status: str = "", admin: dict = Depends(require_admin)) -> list:
    del admin
    ensure_billing_tables()
    sql = (
        "SELECT p.id, p.workspace_id, p.customer_email, u.full_name, p.service_code, p.tier, p.mode, p.total_amount, p.currency, "
        "p.status, p.source, p.payment_date, p.razorpay_payment_id, p.razorpay_payment_link_id, p.created_at "
        "FROM payment_records p LEFT JOIN users u ON u.id = p.user_id"
    )
    params: list = []
    where = []
    if status:
        if status not in PAYMENT_STATUSES:
            raise HTTPException(400, "Unknown payment status.")
        where.append("p.status = ?")
        params.append(status)
    if q.strip():
        where.append("(p.customer_email LIKE ? OR p.id LIKE ? OR p.razorpay_payment_id LIKE ? OR p.razorpay_payment_link_id LIKE ? OR u.full_name LIKE ?)")
        like = f"%{q.strip()}%"
        params.extend([like, like, like, like, like])
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.created_at DESC LIMIT 100"
    rows = query(sql, tuple(params))
    for row in rows:
        row["total_amount"] = float(row["total_amount"])
    return rows


@admin_router.get("/payments/{payment_id}")
def admin_payment(payment_id: str, admin: dict = Depends(require_admin)) -> dict:
    del admin
    row = one("SELECT * FROM payment_records WHERE id = ?", (payment_id,))
    if not row:
        raise HTTPException(404, "Payment not found.")
    items = query("SELECT service_code, tier, mode, list_price, discount_amount, net_amount FROM payment_items WHERE payment_id = ?", (payment_id,))
    for item in items:
        for key in ("list_price", "discount_amount", "net_amount"):
            item[key] = float(item[key])
    return _payment_public(row, items)


def _lock_payment(cur, payment_id: str) -> dict:
    cur.execute("SELECT * FROM payment_records WHERE id = %s FOR UPDATE", (payment_id,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(404, "Payment not found.")
    return dict(row)


@admin_router.post("/payments/{payment_id}/verify-activate")
def verify_and_activate(payment_id: str, payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    ensure_billing_tables()
    razorpay_payment_id = str(payload.get("razorpay_payment_id") or "").strip()[:80]
    method = str(payload.get("payment_method") or "razorpay_link").strip()[:40]
    activated = []
    with db() as connection:
        with connection.cursor() as cur:
            payment = _lock_payment(cur, payment_id)
            if payment["status"] in {"refunded", "cancelled", "failed"}:
                raise HTTPException(409, f"Cannot activate a {payment['status']} payment.")
            if payment["status"] == "verified":
                cur.execute(
                    "SELECT id AS entitlement_id, service_code, tier, expires_at FROM workspace_entitlements WHERE payment_id = %s",
                    (payment_id,),
                )
                activated = [dict(row) for row in cur.fetchall() or []]
                already = True
            else:
                cur.execute("SELECT * FROM payment_items WHERE payment_id = %s", (payment_id,))
                items = [dict(item) for item in cur.fetchall() or []]
                if not items:
                    raise HTTPException(400, "Payment has no service lines.")
                stamp = now_iso()
                cur.execute(
                    "UPDATE payment_records SET status = 'verified', payment_method = %s, razorpay_payment_id = %s, payment_date = %s, "
                    "verified_by = %s, verified_at = %s, source = CASE WHEN source = '' THEN 'admin' ELSE source END, updated_at = %s WHERE id = %s",
                    (method, razorpay_payment_id or payment.get("razorpay_payment_id") or "", stamp, admin["user_id"], stamp, stamp, payment_id),
                )
                activated = _activate_on(cur, payment["workspace_id"], items, payment_id, admin["user_id"])
                already = False
    if not already:
        audit_admin(admin, "verify_activate", "payment", payment_id, {"status": payment["status"]}, {"status": "verified", "activated": activated}, request)
    return {"ok": True, "payment_id": payment_id, "status": "verified", "activated": activated, "already_verified": already}


@admin_router.post("/payments")
def create_manual_payment(payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    ensure_billing_tables()
    email = str(payload.get("customer_email") or "").strip().lower()
    user = one("SELECT id, workspace_id, email, full_name FROM users WHERE email = ?", (email,))
    if not user:
        raise HTTPException(404, "No customer with that email.")
    quote_raw = build_quote(_normalize_items(payload.get("items")))
    if payload.get("total_amount") not in (None, ""):
        quoted = quote_raw["total_amount"]
        stated = _money(payload.get("total_amount"))
        if stated != quoted:
            raise HTTPException(400, f"Stated total {stated} does not match the catalog quote {quoted}.")
    payment_id = new_id()
    insert(
        "payment_records",
        {
            "id": payment_id,
            "user_id": user["id"],
            "customer_email": user["email"],
            "service_code": quote_raw["items"][0]["service_code"] if len(quote_raw["items"]) == 1 else "bundle",
            "tier": quote_raw["items"][0]["tier"] if len(quote_raw["items"]) == 1 else "",
            "mode": quote_raw["items"][0]["mode"] if len(quote_raw["items"]) == 1 else "",
            "amount": quote_raw["subtotal"],
            "discount_amount": quote_raw["discount_amount"],
            "platform_fee": quote_raw["platform_fee"],
            "gst_amount": quote_raw["gst_amount"],
            "total_amount": quote_raw["total_amount"],
            "payment_method": str(payload.get("payment_method") or "manual")[:40],
            "razorpay_payment_id": str(payload.get("razorpay_payment_id") or "")[:80],
            "status": "received",
            "payment_date": str(payload.get("payment_date") or now_iso()),
            "source": "manual",
            "notes": str(payload.get("notes") or "")[:1000],
            "quote_json": json_text(_public_quote(quote_raw)),
            "updated_at": now_iso(),
        },
        user["workspace_id"],
    )
    for item in quote_raw["items"]:
        insert(
            "payment_items",
            {
                "payment_id": payment_id,
                "service_code": item["service_code"],
                "tier": item["tier"],
                "mode": item["mode"],
                "list_price": item["list_price"],
                "discount_amount": item["discount_amount"],
                "net_amount": item["net_amount"],
            },
            user["workspace_id"],
        )
    audit_admin(admin, "manual_payment", "payment", payment_id, {}, {"email": email, "total": float(quote_raw["total_amount"])}, request)
    return {"id": payment_id, "status": "received", "quote": _public_quote(quote_raw)}


@admin_router.get("/customers")
def admin_customers(q: str = "", admin: dict = Depends(require_admin)) -> list:
    del admin
    ensure_billing_tables()
    sql = (
        "SELECT u.id, u.workspace_id, u.email, u.full_name, u.is_verified, u.role, u.created_at, "
        "c.company_name FROM users u LEFT JOIN company_profiles c ON c.workspace_id = u.workspace_id "
    )
    params: tuple = ()
    if q.strip():
        sql += "WHERE u.email LIKE ? OR u.full_name LIKE ? OR c.company_name LIKE ? "
        like = f"%{q.strip()}%"
        params = (like, like, like)
    sql += "ORDER BY u.created_at DESC LIMIT 50"
    rows = query(sql, params)
    for row in rows:
        row["is_verified"] = bool(row.get("is_verified"))
    return rows


def _account_summaries(workspace_id: str) -> list[dict]:
    summaries = []
    specs = [
        ("email", "email_connections", "email_address", "secret"),
        ("whatsapp", "whatsapp_connections", "phone_number_id", "access_token"),
        ("sms", "sms_connections", "sender_id", "secret"),
        ("meta_ads", "meta_ad_accounts", "ad_account_id", "access_token"),
    ]
    for platform, table, identifier, secret in specs:
        try:
            rows = query(f"SELECT id, name, status, {identifier} AS identifier, {secret} AS secret FROM {table} WHERE workspace_id = ?", (workspace_id,))
        except Exception:
            continue
        for row in rows:
            summaries.append({
                "platform": platform,
                "name": row.get("name") or "",
                "identifier": row.get("identifier") or "",
                "status": row.get("status") or "",
                "has_credentials": bool(row.get("secret")),
            })
    try:
        social = query("SELECT platform, handle, name, status, access_token FROM social_accounts WHERE workspace_id = ?", (workspace_id,))
    except Exception:
        social = []
    for row in social:
        summaries.append({
            "platform": row.get("platform") or "social",
            "name": row.get("name") or "",
            "identifier": row.get("handle") or "",
            "status": row.get("status") or "",
            "has_credentials": bool(row.get("access_token")),
        })
    try:
        ads = query("SELECT platform, advertiser_id, name, status, credentials FROM ad_network_accounts WHERE workspace_id = ?", (workspace_id,))
    except Exception:
        ads = []
    for row in ads:
        summaries.append({
            "platform": row.get("platform") or "ads",
            "name": row.get("name") or "",
            "identifier": row.get("advertiser_id") or "",
            "status": row.get("status") or "",
            "has_credentials": bool(row.get("credentials")),
        })
    return summaries


@admin_router.get("/customers/{user_id}")
def admin_customer(user_id: str, admin: dict = Depends(require_admin)) -> dict:
    del admin
    user = one("SELECT id, workspace_id, email, full_name, is_verified, role, created_at FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "Customer not found.")
    profile = one("SELECT company_name, website, industry FROM company_profiles WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (user["workspace_id"],)) or {}
    entitlements = query(
        "SELECT id, service_code, tier, mode, status, starts_at, expires_at, quota_units, source, manual_reason, payment_id FROM workspace_entitlements WHERE workspace_id = ? ORDER BY service_code",
        (user["workspace_id"],),
    )
    payments = query(
        "SELECT id, service_code, tier, total_amount, status, source, payment_date, created_at FROM payment_records WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 30",
        (user["workspace_id"],),
    )
    for row in payments:
        row["total_amount"] = float(row["total_amount"])
    return {
        "user": {**user, "is_verified": bool(user.get("is_verified"))},
        "company": profile,
        "entitlements": entitlements,
        "payments": payments,
        "accounts": _account_summaries(user["workspace_id"]),
    }


@admin_router.post("/customers/{user_id}/grant")
def grant_service(user_id: str, payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    ensure_billing_tables()
    user = one("SELECT id, workspace_id, email FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "Customer not found.")
    reason = str(payload.get("reason") or "").strip()
    if len(reason) < 3:
        raise HTTPException(400, "A reason is required for manual access.")
    plan = _plan(str(payload.get("service_code") or ""), str(payload.get("tier") or ""), str(payload.get("mode") or "standard"))
    days = int(payload.get("days") or TERM_DAYS)
    if days < 1 or days > 366:
        raise HTTPException(400, "Grant length must be 1 to 366 days.")
    entitlement_id = _upsert_entitlement(
        user["workspace_id"],
        {"service_code": plan["service_code"], "tier": plan["tier"], "mode": plan["mode"], "quota_units": plan.get("quota_units") or 0},
        "",
        "admin",
        reason,
        admin["user_id"],
        days,
    )
    audit_admin(admin, "grant", "entitlement", entitlement_id, {}, {"email": user["email"], "service_code": plan["service_code"], "tier": plan["tier"], "days": days, "reason": reason}, request)
    return {"ok": True, "id": entitlement_id}


@admin_router.post("/customers/{user_id}/extend")
def extend_service(user_id: str, payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    user = one("SELECT id, workspace_id FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "Customer not found.")
    service_code = str(payload.get("service_code") or "").strip()
    days = int(payload.get("days") or 0)
    if days < 1 or days > 366:
        raise HTTPException(400, "Extension must be 1 to 366 days.")
    row = one(
        "SELECT * FROM workspace_entitlements WHERE workspace_id = ? AND service_code = ? ORDER BY expires_at DESC LIMIT 1",
        (user["workspace_id"], service_code),
    )
    if not row:
        raise HTTPException(404, "Entitlement not found.")
    base = row["expires_at"] if str(row.get("expires_at") or "") > now_iso() else now_iso()
    expires = _plus_days(days, base)
    execute(
        "UPDATE workspace_entitlements SET status = 'active', expires_at = ?, updated_at = ? WHERE id = ?",
        (expires, now_iso(), row["id"]),
    )
    audit_admin(admin, "extend", "entitlement", row["id"], {"expires_at": row["expires_at"]}, {"expires_at": expires, "days": days}, request)
    return {"ok": True, "expires_at": expires}


@admin_router.post("/customers/{user_id}/suspend")
def suspend_service(user_id: str, payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    user = one("SELECT workspace_id FROM users WHERE id = ?", (user_id,))
    if not user:
        raise HTTPException(404, "Customer not found.")
    service_code = str(payload.get("service_code") or "").strip()
    status = str(payload.get("status") or "suspended")
    if status not in {"suspended", "cancelled", "active"}:
        raise HTTPException(400, "Status must be suspended, cancelled, or active.")
    row = one(
        "SELECT * FROM workspace_entitlements WHERE workspace_id = ? AND service_code = ? ORDER BY expires_at DESC LIMIT 1",
        (user["workspace_id"], service_code),
    )
    if not row:
        raise HTTPException(404, "Entitlement not found.")
    execute("UPDATE workspace_entitlements SET status = ?, updated_at = ? WHERE id = ?", (status, now_iso(), row["id"]))
    audit_admin(admin, status, "entitlement", row["id"], {"status": row["status"]}, {"status": status, "reason": str(payload.get("reason") or "")[:240]}, request)
    return {"ok": True, "status": status}


@admin_router.post("/plans/link")
def attach_payment_link(payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    plan = _plan(str(payload.get("service_code") or ""), str(payload.get("tier") or ""), str(payload.get("mode") or "standard"))
    link_id = str(payload.get("razorpay_payment_link_id") or "").strip()[:80]
    link_url = str(payload.get("razorpay_payment_link_url") or "").strip()[:500]
    if link_url and not link_url.startswith("https://"):
        raise HTTPException(400, "Payment link URL must be https.")
    execute(
        "UPDATE service_plans SET razorpay_payment_link_id = ?, razorpay_payment_link_url = ?, updated_at = ? WHERE id = ?",
        (link_id, link_url, now_iso(), plan["id"]),
    )
    audit_admin(admin, "attach_link", "service_plan", plan["id"], {}, {"service_code": plan["service_code"], "tier": plan["tier"], "mode": plan["mode"], "link_id": link_id}, request)
    return {"ok": True}


@admin_router.get("/plans")
def admin_plans(admin: dict = Depends(require_admin)) -> list:
    del admin
    ensure_billing_tables()
    rows = query(
        "SELECT p.id, p.service_code, c.name, p.tier, p.mode, p.price, p.price_version, p.quota_units, p.active, "
        "p.razorpay_payment_link_id, p.razorpay_payment_link_url FROM service_plans p "
        "JOIN service_catalog c ON c.service_code = p.service_code WHERE p.country = 'IN' "
        "ORDER BY c.name, p.mode, FIELD(p.tier, 'starter', 'growth', 'pro')"
    )
    for row in rows:
        row["price"] = float(row["price"])
        row["active"] = bool(row["active"])
        row["sellable"] = bool(row["active"] and row["razorpay_payment_link_url"])
    return rows


@admin_router.post("/plans/price")
def confirm_price(payload: dict, request: Request, admin: dict = Depends(require_admin)) -> dict:
    ensure_billing_tables()
    code = str(payload.get("service_code") or "").strip()
    tier = str(payload.get("tier") or "").strip().lower()
    mode = str(payload.get("mode") or "standard").strip().lower()
    row = one(
        "SELECT * FROM service_plans WHERE service_code = ? AND tier = ? AND mode = ? AND country = 'IN'",
        (code, tier, mode),
    )
    if not row:
        raise HTTPException(404, "Plan not found.")
    price = _money(payload.get("price"))
    if price <= 0:
        raise HTTPException(400, "Price must be greater than zero.")
    quota = int(payload.get("quota_units") or 0)
    active = 1 if payload.get("active", True) else 0
    execute(
        "UPDATE service_plans SET price = ?, quota_units = ?, price_version = 'confirmed', active = ?, updated_at = ? WHERE id = ?",
        (price, quota, active, now_iso(), row["id"]),
    )
    audit_admin(admin, "confirm_price", "service_plan", row["id"], {"price": float(row["price"]), "price_version": row["price_version"]}, {"price": float(price), "active": bool(active), "quota_units": quota}, request)
    return {"ok": True, "price_version": "confirmed", "active": bool(active)}


@admin_router.get("/audit")
def admin_audit(admin: dict = Depends(require_admin)) -> list:
    del admin
    ensure_billing_tables()
    return query(
        "SELECT id, admin_user_id, action, target_type, target_id, ip_address, created_at FROM admin_audit_log ORDER BY created_at DESC LIMIT 100"
    )


@admin_router.get("/health")
def admin_health(admin: dict = Depends(require_admin)) -> dict:
    del admin
    database = "healthy"
    try:
        one("SELECT 1 AS ok")
    except Exception:
        database = "down"
    worker = ""
    try:
        job = one("SELECT last_success_at, status FROM data_refresh_jobs WHERE job_type = 'outreach' ORDER BY last_success_at DESC LIMIT 1")
        worker = (job or {}).get("last_success_at") or ""
    except Exception:
        worker = ""
    last_payment = one("SELECT created_at FROM payment_records ORDER BY created_at DESC LIMIT 1")
    return {
        "api": "healthy",
        "database": database,
        "worker_last_success": worker,
        "razorpay": "configured" if env("RAZORPAY_KEY_ID") and env("RAZORPAY_KEY_SECRET") else "manual",
        "webhook": "configured" if env("RAZORPAY_WEBHOOK_SECRET") else "not_configured",
        "admin_email_configured": bool(env("R360_ADMIN_EMAIL")),
        "encryption": "required",
        "last_payment_at": (last_payment or {}).get("created_at") or "",
        "secrets_displayed": False,
    }


@admin_router.post("/webhooks/razorpay")
async def razorpay_webhook(request: Request) -> dict:
    """Automation only. Signature is required. Activation stays behind an explicit flag."""
    import hashlib
    import hmac
    secret = env("RAZORPAY_WEBHOOK_SECRET", "")
    raw = await request.body()
    signature = request.headers.get("x-razorpay-signature") or ""
    if not secret or not signature:
        raise HTTPException(503, "Razorpay webhook is not configured.")
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature.strip()):
        raise HTTPException(403, "Webhook signature mismatch.")
    payload = json.loads(raw.decode() or "{}")
    event = str(payload.get("event") or "")
    entity = ((payload.get("payload") or {}).get("payment") or {}).get("entity") or {}
    notes = entity.get("notes") or {}
    payment_id = str(notes.get("payment_record_id") or "")
    if not payment_id:
        return {"ok": True, "ignored": True}
    status = "received" if event == "payment.captured" else "failed" if event == "payment.failed" else ""
    if not status:
        return {"ok": True, "ignored": True}
    execute(
        "UPDATE payment_records SET status = ?, razorpay_payment_id = ?, source = 'webhook', updated_at = ? WHERE id = ? AND status = 'pending'",
        (status, str(entity.get("id") or "")[:80], now_iso(), payment_id),
    )
    activated = False
    if status == "received" and env_flag("R360_RAZORPAY_AUTO_ACTIVATE"):
        verify_and_activate(payment_id, {"razorpay_payment_id": entity.get("id") or "", "payment_method": "razorpay"}, request, {"user_id": "webhook", "role": "admin", "email": env("R360_ADMIN_EMAIL", "")})
        activated = True
    return {"ok": True, "status": status, "activated": activated}