from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
import requests

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import learning
from social_automations import retry_comment_events
from db import db, env, execute, hours_from_now, init_db, insert, json_text, new_id, now_iso, one, query, require_encryption, reveal
from engine import process_agent_due, process_due, process_due_campaigns
from ad_platforms import run_optimization_rules
from search_intelligence import process_search_runs
from cloud import cloud_worker
import smtplib
from email.message import EmailMessage
from fastapi import APIRouter, Depends, HTTPException
from auth import user_from_header

def _publish_linkedin(account: dict, caption: str, media: str = "") -> dict:
    connection = reveal("social_accounts", account) or {}
    token = connection.get("access_token") or ""
    if not token:
        raise ValueError("LinkedIn account has no access token.")
    if media:
        raise ValueError("LinkedIn image and video upload is not connected yet; queue this as a text-only post.")
    author = str(connection.get("handle") or "").strip()
    try:
        settings = json.loads(connection.get("settings") or "{}")
    except (TypeError, json.JSONDecodeError):
        settings = {}
    author = str(settings.get("author_urn") or author).strip()
    if not re.fullmatch(r"urn:li:organization:[0-9]+|urn:li:person:[A-Za-z0-9_-]+", author):
        raise ValueError("Use a LinkedIn author URN such as urn:li:organization:12345 or urn:li:person:<member-id>.")
    if not caption.strip() or len(caption) > 3000:
        raise ValueError("LinkedIn text posts must contain 1 to 3,000 characters.")
    version = str(settings.get("api_version") or env("LINKEDIN_MARKETING_VERSION", "202608"))
    try:
        response = requests.post(
            "https://api.linkedin.com/rest/posts",
            headers={
                "Authorization": f"Bearer {token}",
                "Linkedin-Version": version,
                "X-Restli-Protocol-Version": "2.0.0",
                "Content-Type": "application/json",
            },
            json={
                "author": author,
                "commentary": caption,
                "visibility": "PUBLIC",
                "distribution": {
                    "feedDistribution": "MAIN_FEED",
                    "targetEntities": [],
                    "thirdPartyDistributionChannels": [],
                },
                "lifecycleState": "PUBLISHED",
                "isReshareDisabledByAuthor": False,
            },
            timeout=25,
        )
    except requests.RequestException as error:
        raise ValueError(f"LinkedIn request failed ({type(error).__name__}).") from error
    if not response.ok:
        raise ValueError(f"LinkedIn returned HTTP {response.status_code}.")
    return {"post_id": response.headers.get("x-restli-id", ""), "status": "published"}


def _account_settings(account: dict) -> dict:
    try:
        settings = json.loads(account.get("settings") or "{}")
        return settings if isinstance(settings, dict) else {}
    except (TypeError, json.JSONDecodeError):
        return {}


def _publish_instagram_reel(account: dict, post: dict, attempt: dict) -> dict:
    connection = reveal("social_accounts", account) or {}
    token = str(connection.get("access_token") or "")
    user_id = str(connection.get("handle") or "")
    if not token or not user_id:
        raise ValueError("Instagram account has no professional account ID or access token.")
    video_url = str(post.get("media") or "").strip()
    if not video_url or urlparse(video_url).scheme != "https" or not urlparse(video_url).netloc:
        raise ValueError("Instagram Reels need a public HTTPS video URL that Meta can fetch.")
    settings = _account_settings(connection)
    version = str(settings.get("api_version") or env("META_GRAPH_API_VERSION", "v25.0")).strip("/")
    host = "graph.instagram.com" if str(settings.get("login_type") or "facebook").lower() == "instagram" else "graph.facebook.com"
    base = f"https://{host}/{version}"
    headers = {"Authorization": f"Bearer {token}"}
    creation_id = str(attempt.get("provider_ref") or "")
    if not creation_id:
        try:
            response = requests.post(
                f"{base}/{user_id}/media",
                headers=headers,
                data={"media_type": "REELS", "video_url": video_url, "caption": str(post.get("caption") or ""), "share_to_feed": "true"},
                timeout=30,
            )
        except requests.RequestException as error:
            raise ValueError(f"Instagram container request failed ({type(error).__name__}).") from error
        if not response.ok:
            raise ValueError(f"Instagram returned HTTP {response.status_code} while creating the Reel container.")
        creation_id = str((response.json() or {}).get("id") or "")
        if not creation_id:
            raise ValueError("Instagram did not return a Reel container ID.")
        execute(
            "UPDATE social_publish_attempts SET provider_ref = ?, attempts = attempts + 1, detail = ?, updated_at = ? WHERE id = ?",
            (creation_id, "Instagram is processing the Reel video.", now_iso(), attempt["id"]),
        )
        return {"status": "pending", "detail": "Instagram is processing the Reel video."}

    try:
        response = requests.get(
            f"{base}/{creation_id}", headers=headers,
            params={"fields": "status_code,status"}, timeout=20,
        )
    except requests.RequestException as error:
        raise ValueError(f"Instagram status request failed ({type(error).__name__}).") from error
    if not response.ok:
        raise ValueError(f"Instagram returned HTTP {response.status_code} while checking Reel processing.")
    state = str((response.json() or {}).get("status_code") or "").upper()
    if state in {"IN_PROGRESS", "STARTED"}:
        if int(attempt.get("attempts") or 0) >= 120:
            raise ValueError("Instagram Reel processing did not finish after 120 checks.")
        execute(
            "UPDATE social_publish_attempts SET attempts = attempts + 1, detail = ?, updated_at = ? WHERE id = ?",
            (f"Instagram Reel processing: {state}.", now_iso(), attempt["id"]),
        )
        return {"status": "pending", "detail": f"Instagram Reel processing: {state}."}
    if state != "FINISHED":
        raise ValueError(f"Instagram Reel processing ended with status {state or 'unknown'}.")
    try:
        published = requests.post(
            f"{base}/{user_id}/media_publish", headers=headers,
            data={"creation_id": creation_id}, timeout=30,
        )
    except requests.RequestException as error:
        raise ValueError(f"Instagram publish request failed ({type(error).__name__}).") from error
    if not published.ok:
        raise ValueError(f"Instagram returned HTTP {published.status_code} while publishing the Reel.")
    return {"status": "published", "post_id": str((published.json() or {}).get("id") or "")}


def _publish_threads_text(account: dict, post: dict, attempt: dict) -> dict:
    connection = reveal("social_accounts", account) or {}
    token = str(connection.get("access_token") or "")
    user_id = str(connection.get("handle") or "")
    if not token or not user_id:
        raise ValueError("Threads account has no user ID or access token.")
    if post.get("media"):
        raise ValueError("Threads scheduled publishing currently supports text posts only.")
    settings = _account_settings(connection)
    version = str(settings.get("api_version") or "v1.0").strip("/")
    base = f"https://graph.threads.net/{version}"
    creation_id = str(attempt.get("provider_ref") or "")
    if not creation_id:
        try:
            created = requests.post(
                f"{base}/{user_id}/threads",
                data={"media_type": "TEXT", "text": str(post.get("caption") or ""), "access_token": token},
                timeout=25,
            )
        except requests.RequestException as error:
            raise ValueError(f"Threads request failed ({type(error).__name__}).") from error
        if not created.ok:
            raise ValueError(f"Threads returned HTTP {created.status_code} while creating the post.")
        creation_id = str((created.json() or {}).get("id") or "")
        if not creation_id:
            raise ValueError("Threads did not return a post container ID.")
        execute(
            "UPDATE social_publish_attempts SET provider_ref = ?, attempts = attempts + 1, detail = ?, updated_at = ? WHERE id = ?",
            (creation_id, "Threads post container created.", now_iso(), attempt["id"]),
        )
    try:
        published = requests.post(
            f"{base}/{user_id}/threads_publish",
            data={"creation_id": creation_id, "access_token": token}, timeout=25,
        )
    except requests.RequestException as error:
        raise ValueError(f"Threads publish request failed ({type(error).__name__}).") from error
    if not published.ok:
        raise ValueError(f"Threads returned HTTP {published.status_code} while publishing.")
    return {"status": "published", "post_id": str((published.json() or {}).get("id") or "")}


def process_scheduled_posts(limit: int = 30) -> dict:
    """Publish supported due posts and resume provider media containers on later worker ticks."""
    due = query(
        "SELECT * FROM scheduled_posts WHERE status = 'queued' AND scheduled_at <= ? ORDER BY scheduled_at ASC LIMIT ?",
        (now_iso(), limit),
    )
    published = failed = partial = queued = 0
    for post in due:
        try:
            account_ids = json.loads(post.get("account_ids") or "[]")
        except (TypeError, json.JSONDecodeError):
            account_ids = []
        if not isinstance(account_ids, list):
            account_ids = []
        outcomes = []
        caption = str(post.get("caption") or "")
        for account_id in account_ids:
            account = one(
                "SELECT * FROM social_accounts WHERE id = ? AND workspace_id = ?",
                (account_id, post["workspace_id"]),
            )
            if not account:
                outcomes.append({"account_id": account_id, "status": "failed", "detail": "social account unavailable"})
                continue
            attempt = one("SELECT * FROM social_publish_attempts WHERE scheduled_post_id = ? AND account_id = ?", (post["id"], account_id))
            if not attempt:
                insert("social_publish_attempts", {
                    "scheduled_post_id": post["id"], "account_id": account_id,
                    "platform": account.get("platform") or "", "status": "pending", "updated_at": now_iso(),
                }, post["workspace_id"])
                attempt = one("SELECT * FROM social_publish_attempts WHERE scheduled_post_id = ? AND account_id = ?", (post["id"], account_id))
            if attempt.get("status") in {"published", "failed"}:
                outcomes.append({"account_id": account_id, "platform": account.get("platform"), "status": attempt["status"], "post_id": attempt.get("response_id"), "detail": attempt.get("detail") or ""})
                continue
            try:
                platform = account.get("platform")
                if platform == "linkedin":
                    result = _publish_linkedin(account, caption, str(post.get("media") or ""))
                elif platform == "instagram":
                    result = _publish_instagram_reel(account, post, attempt)
                elif platform == "threads":
                    result = _publish_threads_text(account, post, attempt)
                else:
                    raise ValueError("Live publishing adapter is not connected for this platform.")
                state = result.get("status") or "failed"
                if state == "published":
                    execute("UPDATE social_publish_attempts SET status = 'published', response_id = ?, attempts = attempts + 1, detail = '', updated_at = ? WHERE id = ?", (result.get("post_id") or "", now_iso(), attempt["id"]))
                else:
                    execute("UPDATE social_publish_attempts SET status = 'pending', detail = ?, updated_at = ? WHERE id = ?", (result.get("detail") or "Provider processing.", now_iso(), attempt["id"]))
                outcomes.append({"account_id": account_id, "platform": platform, **result})
            except ValueError as error:
                execute("UPDATE social_publish_attempts SET status = 'failed', attempts = attempts + 1, detail = ?, updated_at = ? WHERE id = ?", (str(error)[:500], now_iso(), attempt["id"]))
                outcomes.append({"account_id": account_id, "platform": account.get("platform"), "status": "failed", "detail": str(error)[:240]})
        successes = sum(1 for outcome in outcomes if outcome.get("status") == "published")
        pending = sum(1 for outcome in outcomes if outcome.get("status") == "pending")
        if pending:
            status = "queued"
            queued += 1
        elif successes == len(account_ids) and successes:
            status = "published"
            published += 1
        elif successes:
            status = "partial"
            partial += 1
        else:
            status = "failed"
            failed += 1
        if not account_ids:
            outcomes = [{"status": "failed", "detail": "missing social account selection"}]
        execute(
            "UPDATE scheduled_posts SET status = ?, detail = ? WHERE id = ? AND workspace_id = ?",
            (status, json.dumps(outcomes, ensure_ascii=False), post["id"], post["workspace_id"]),
        )
    return {"scanned": len(due), "published": published, "partial": partial, "queued": queued, "failed": failed}

JOB_HOURS = {
    "outreach": 1,
    "campaigns": 1,
    "agent": 1,
    "scheduled_posts": 1,
    "social_comment_retries": 1,
    "ad_optimization": 24,
    "search_intelligence": 24,
    "cloud_catalog": 24,
    "learning": 24,
}
WORKSPACE_JOBS = ("outreach", "campaigns")
SYSTEM_JOBS = ("agent", "scheduled_posts", "social_comment_retries", "ad_optimization", "search_intelligence", "cloud_catalog", "learning")
STALE_HOURS = 2

router = APIRouter(prefix="/api/refresh", tags=["refresh"])
refresh_router = router


def ensure_refresh_tables() -> None:
    execute(
        "CREATE TABLE IF NOT EXISTS data_refresh_jobs ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL DEFAULT '', "
        "job_type VARCHAR(60) NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'idle', "
        "refresh_interval_hours DOUBLE NOT NULL DEFAULT 24, "
        "last_started_at VARCHAR(40) NOT NULL DEFAULT '', last_completed_at VARCHAR(40) NOT NULL DEFAULT '', "
        "next_run_at VARCHAR(40) NOT NULL DEFAULT '', last_success_at VARCHAR(40) NOT NULL DEFAULT '', "
        "last_error_at VARCHAR(40) NOT NULL DEFAULT '', last_error VARCHAR(500) NOT NULL DEFAULT '', "
        "records_processed INT NOT NULL DEFAULT 0, records_changed INT NOT NULL DEFAULT 0, "
        "created_at VARCHAR(40) NOT NULL DEFAULT '', updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_refresh_job (workspace_id, job_type))"
    )
    execute(
        "CREATE TABLE IF NOT EXISTS refresh_requests ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, user_id VARCHAR(36) NOT NULL DEFAULT '', "
        "module VARCHAR(60) NOT NULL, current_frequency VARCHAR(40) NOT NULL DEFAULT '', "
        "requested_frequency VARCHAR(40) NOT NULL, reason VARCHAR(1000) NOT NULL DEFAULT '', "
        "business_impact VARCHAR(1000) NOT NULL DEFAULT '', comments VARCHAR(1000) NOT NULL DEFAULT '', "
        "priority VARCHAR(20) NOT NULL DEFAULT 'normal', status VARCHAR(20) NOT NULL DEFAULT 'pending', "
        "admin_notes VARCHAR(1000) NOT NULL DEFAULT '', notify_status VARCHAR(40) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL DEFAULT '', reviewed_at VARCHAR(40) NOT NULL DEFAULT '', "
        "implemented_at VARCHAR(40) NOT NULL DEFAULT '', INDEX idx_refresh_req (workspace_id, created_at))"
    )
    execute(
        "CREATE TABLE IF NOT EXISTS audit_events ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL DEFAULT '', "
        "user_id VARCHAR(36) NOT NULL DEFAULT '', event_type VARCHAR(60) NOT NULL, "
        "entity_type VARCHAR(40) NOT NULL DEFAULT '', entity_id VARCHAR(36) NOT NULL DEFAULT '', "
        "actor_type VARCHAR(20) NOT NULL DEFAULT 'system', actor_id VARCHAR(36) NOT NULL DEFAULT '', "
        "status VARCHAR(40) NOT NULL DEFAULT '', description VARCHAR(500) NOT NULL DEFAULT '', "
        "metadata_json LONGTEXT, created_at VARCHAR(40) NOT NULL, INDEX idx_audit_ws (workspace_id, created_at))"
    )


def audit(workspace_id: str, event_type: str, description: str, **fields) -> None:
    execute(
        "INSERT INTO audit_events (id, workspace_id, user_id, event_type, entity_type, entity_id, actor_type, actor_id, status, description, metadata_json, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            new_id(),
            workspace_id or "",
            fields.get("user_id") or "",
            event_type,
            fields.get("entity_type") or "",
            fields.get("entity_id") or "",
            fields.get("actor_type") or "system",
            fields.get("actor_id") or "",
            fields.get("status") or "",
            description[:500],
            json_text(fields.get("metadata") or {}),
            now_iso(),
        ),
    )


def _interval(job_type: str) -> float:
    override = env(f"R360_JOB_{job_type.upper()}_HOURS", "")
    try:
        return float(override) if override else float(JOB_HOURS[job_type])
    except ValueError:
        return float(JOB_HOURS[job_type])


def _ensure_job(workspace_id: str, job_type: str) -> None:
    if one("SELECT id FROM data_refresh_jobs WHERE workspace_id = ? AND job_type = ?", (workspace_id, job_type)):
        return
    execute(
        "INSERT INTO data_refresh_jobs (id, workspace_id, job_type, status, refresh_interval_hours, next_run_at, created_at, updated_at) VALUES (?, ?, ?, 'idle', ?, ?, ?, ?)",
        (new_id(), workspace_id, job_type, _interval(job_type), now_iso(), now_iso(), now_iso()),
    )


def seed_jobs() -> None:
    ensure_refresh_tables()
    for job_type in SYSTEM_JOBS:
        _ensure_job("", job_type)
    for row in query("SELECT id FROM workspaces"):
        for job_type in WORKSPACE_JOBS:
            _ensure_job(row["id"], job_type)


def release_stale() -> int:
    cutoff = hours_from_now(-STALE_HOURS)
    rows = query("SELECT id FROM data_refresh_jobs WHERE status = 'running' AND last_started_at <> '' AND last_started_at <= ?", (cutoff,))
    for row in rows:
        execute(
            "UPDATE data_refresh_jobs SET status = 'idle', last_error = 'Stale running job released.', last_error_at = ?, updated_at = ? WHERE id = ?",
            (now_iso(), now_iso(), row["id"]),
        )
    return len(rows)


def claim_due(limit: int = 3) -> list[dict]:
    due = query(
        "SELECT * FROM data_refresh_jobs WHERE status <> 'running' AND next_run_at <> '' AND next_run_at <= ? ORDER BY next_run_at ASC LIMIT ?",
        (now_iso(), limit),
    )
    claimed = []
    stamp = now_iso()
    for job in due:
        with db() as connection:
            with connection.cursor() as cur:
                cur.execute(
                    "UPDATE data_refresh_jobs SET status = 'running', last_started_at = %s, updated_at = %s "
                    "WHERE id = %s AND status <> 'running' AND next_run_at <= %s",
                    (stamp, stamp, job["id"], stamp),
                )
                if cur.rowcount == 1:
                    job["status"] = "running"
                    job["last_started_at"] = stamp
                    claimed.append(job)
    return claimed


def finish_job(job: dict, outcome: dict | None = None, error: str = "") -> None:
    outcome = outcome or {}
    processed = int(outcome.get("processed") or outcome.get("scanned") or outcome.get("checked") or 0)
    changed = int(outcome.get("changed") or outcome.get("sent") or outcome.get("published") or outcome.get("paused") or 0)
    interval = float(job.get("refresh_interval_hours") or _interval(job["job_type"]))
    if error:
        execute(
            "UPDATE data_refresh_jobs SET status = 'idle', last_completed_at = ?, next_run_at = ?, last_error_at = ?, last_error = ?, records_processed = ?, updated_at = ? WHERE id = ?",
            (now_iso(), hours_from_now(min(interval, 1)), error[:500], error[:500], processed, now_iso(), job["id"]),
        )
        return
    execute(
        "UPDATE data_refresh_jobs SET status = 'idle', last_completed_at = ?, last_success_at = ?, next_run_at = ?, last_error = '', records_processed = ?, records_changed = ?, updated_at = ? WHERE id = ?",
        (now_iso(), now_iso(), hours_from_now(interval), processed, changed, now_iso(), job["id"]),
    )


def public_jobs(workspace_id: str) -> list[dict]:
    ensure_refresh_tables()
    rows = query(
        "SELECT job_type, status, refresh_interval_hours, last_started_at, last_completed_at, last_success_at, next_run_at, last_error, records_processed, records_changed "
        "FROM data_refresh_jobs WHERE workspace_id = ? OR workspace_id = '' ORDER BY job_type",
        (workspace_id,),
    )
    for row in rows:
        row["data_as_of"] = row.get("last_success_at") or row.get("last_completed_at") or ""
        row["note"] = "Based on the last completed refresh, not a live provider call."
    return rows


def _notify_admin(request_row: dict) -> str:
    admin = env("R360_ADMIN_EMAIL", "")
    user = env("SMTP_USER", "")
    password = env("SMTP_PASSWORD", "")
    if not admin or not user or not password:
        return "not_configured"
    message = EmailMessage()
    message["Subject"] = f"Revenue360s refresh request: {request_row['module']}"
    message["From"] = user
    message["To"] = admin
    message.set_content(
        "A customer requested a faster refresh. The database row is the record; this email is only the notification.\n\n"
        f"request_id: {request_row['id']}\n"
        f"workspace_id: {request_row['workspace_id']}\n"
        f"module: {request_row['module']}\n"
        f"current: {request_row['current_frequency']}\n"
        f"requested: {request_row['requested_frequency']}\n"
        f"reason: {request_row['reason']}\n"
        f"impact: {request_row['business_impact']}\n"
        f"comments: {request_row['comments']}\n"
    )
    try:
        with smtplib.SMTP(env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587") or 587), timeout=20) as server:
            server.starttls()
            server.login(user, password)
            server.send_message(message)
        return "sent"
    except (OSError, smtplib.SMTPException):
        return "failed"


@router.get("/jobs")
def list_jobs(user: dict = Depends(user_from_header)) -> dict:
    return {"jobs": public_jobs(user["workspace_id"])}


@router.get("/requests")
def list_requests(user: dict = Depends(user_from_header)) -> list[dict]:
    ensure_refresh_tables()
    return query(
        "SELECT id, module, current_frequency, requested_frequency, reason, business_impact, priority, status, notify_status, created_at, reviewed_at, implemented_at "
        "FROM refresh_requests WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 50",
        (user["workspace_id"],),
    )


@router.post("/requests")
def create_request(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ensure_refresh_tables()
    module = str(payload.get("module") or "").strip()
    requested = str(payload.get("requested_frequency") or "").strip()
    reason = str(payload.get("reason") or "").strip()
    if module not in JOB_HOURS or not requested or not reason:
        raise HTTPException(400, "Module, requested frequency, and reason are required.")
    if len(requested) > 40 or len(reason) > 1000:
        raise HTTPException(400, "Requested frequency or reason is too long.")
    current = one(
        "SELECT refresh_interval_hours FROM data_refresh_jobs WHERE workspace_id = ? AND job_type = ?",
        (user["workspace_id"], module),
    )
    request_id = new_id()
    row = {
        "id": request_id,
        "workspace_id": user["workspace_id"],
        "module": module,
        "current_frequency": f"{current['refresh_interval_hours']}h" if current else "",
        "requested_frequency": requested,
        "reason": reason,
        "business_impact": str(payload.get("business_impact") or "")[:1000],
        "comments": str(payload.get("comments") or "")[:1000],
    }
    execute(
        "INSERT INTO refresh_requests (id, workspace_id, user_id, module, current_frequency, requested_frequency, reason, business_impact, comments, priority, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'normal', 'pending', ?)",
        (request_id, row["workspace_id"], user["user_id"], module, row["current_frequency"], requested, reason, row["business_impact"], row["comments"], now_iso()),
    )
    notify_status = _notify_admin(row)
    execute("UPDATE refresh_requests SET notify_status = ? WHERE id = ?", (notify_status, request_id))
    audit(
        user["workspace_id"],
        "DATA_REFRESH_REQUESTED",
        f"Faster refresh requested for {module}.",
        user_id=user["user_id"],
        entity_type="refresh_request",
        entity_id=request_id,
        actor_type="user",
        actor_id=user["user_id"],
        status="pending",
        metadata={"module": module, "requested_frequency": requested, "notify_status": notify_status},
    )
    return {"id": request_id, "status": "pending", "notify_status": notify_status}


def run_once() -> dict:
    init_db()
    seed_jobs()
    released = release_stale()
    jobs = claim_due(limit=3)
    result = {"released_stale": released, "claimed": len(jobs), "jobs": []}
    for job in jobs:
        handler = _JOBS.get(job["job_type"])
        if not handler:
            finish_job(job, error=f"No handler for {job['job_type']}")
            result["jobs"].append({"job": job["job_type"], "error": "no_handler"})
            continue
        try:
            outcome = handler(job)
            finish_job(job, outcome if isinstance(outcome, dict) else {})
            result["jobs"].append({"job": job["job_type"], "workspace_id": job.get("workspace_id") or "", **(outcome if isinstance(outcome, dict) else {})})
        except Exception as error:
            finish_job(job, error=str(error))
            result["jobs"].append({"job": job["job_type"], "error": str(error)[:200]})
    return result


def _outreach(job: dict) -> dict:
    return process_due(job["workspace_id"] or None)


def _campaigns(job: dict) -> dict:
    return process_due_campaigns(job["workspace_id"] or None)


def _posts(job: dict) -> dict:
    return process_scheduled_posts()


def _comments(job: dict) -> dict:
    return {"processed": retry_comment_events()}


def _ads(job: dict) -> dict:
    return run_optimization_rules()


def _search(job: dict) -> dict:
    return process_search_runs()


def _cloud(job: dict) -> dict:
    return cloud_worker()


def _learning(job: dict) -> dict:
    return learning.learn_nightly()


def _agent(job: dict) -> dict:
    return process_agent_due(job.get("workspace_id") or None)


_JOBS = {
    "outreach": _outreach,
    "campaigns": _campaigns,
    "agent": _agent,
    "scheduled_posts": _posts,
    "social_comment_retries": _comments,
    "ad_optimization": _ads,
    "search_intelligence": _search,
    "cloud_catalog": _cloud,
    "learning": _learning,
}


def main() -> None:
    print("Revenue360s worker started. Ctrl+C to stop.")
    require_encryption()
    while True:
        try:
            print(time.strftime("%Y-%m-%d %H:%M:%S"), run_once())
        except Exception as error:
            print("worker error", error)
        time.sleep(int(env("R360_WORKER_POLL_SECONDS", "60") or 60))

if __name__ == "__main__":
    main()