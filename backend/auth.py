from __future__ import annotations
import hashlib
import secrets
import time
from urllib.parse import urlencode
import requests
from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, UploadFile
from db import (check_password, env, env_flag, execute, hash_password, hours_from_now, insert, new_id, normalize_email, now_iso, one, public_url,)

router = APIRouter(prefix="/api/auth", tags=["auth"])
SESSION_TTL_HOURS = 24 * 14
_ATTEMPTS: dict[str, list[float]] = {}

def _throttle(key: str, limit: int = 8, window: int = 900) -> None:
    now = time.time()
    if len(_ATTEMPTS) > 5000:
        _ATTEMPTS.clear()
    recent = [t for t in _ATTEMPTS.get(key, []) if now - t < window]
    if len(recent) >= limit:
        raise HTTPException(429, "Too many attempts. Try again in a few minutes.")
    recent.append(now)
    _ATTEMPTS[key] = recent

def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()

def current_user(authorization: str | None) -> dict:
    token = (authorization or "").replace("Bearer ", "").strip()
    row = one(
        "SELECT user_id, workspace_id, email, full_name, expires_at FROM sessions WHERE token_hash = ?",
        (_hash_token(token),),
    ) if token else None
    if not row or row["expires_at"] < now_iso():
        raise HTTPException(401, "Sign in first.")
    return {key: row[key] for key in ("user_id", "workspace_id", "email", "full_name")}

def user_from_header(authorization: str | None = Header(default=None)) -> dict:
    return current_user(authorization)

def _session_payload(user: dict) -> dict:
    token = secrets.token_urlsafe(32)
    session = {
        "user_id": user["id"],
        "workspace_id": user["workspace_id"],
        "email": user["email"],
        "full_name": user.get("full_name") or "",
    }
    execute("DELETE FROM sessions WHERE expires_at < ?", (now_iso(),))
    execute(
        "INSERT INTO sessions (token_hash, user_id, workspace_id, email, full_name, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_hash_token(token), session["user_id"], session["workspace_id"], session["email"], session["full_name"], hours_from_now(SESSION_TTL_HOURS), now_iso()),
    )
    return {"token": token, **session}

@router.post("/signup")
def signup(payload: dict) -> dict:
    email = normalize_email(payload.get("email", ""))
    password = str(payload.get("password") or "")
    full_name = str(payload.get("full_name") or "").strip()
    if not email or "@" not in email or len(password) < 8 or not full_name:
        raise HTTPException(400, "Name, valid email, and 8+ character password required.")
    if one("SELECT id FROM users WHERE email = ?", (email,)):
        raise HTTPException(400, "An account with that email already exists.")
    workspace = new_id()
    execute("INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)", (workspace, f"{full_name}'s workspace", now_iso()))
    code = f"{secrets.randbelow(1_000_000):06d}"
    insert(
        "users",
        {
            "workspace_id": workspace,
            "email": email,
            "password_hash": hash_password(password),
            "full_name": full_name,
            "is_verified": 0,
            "verification_token": code,
            "verification_expires_at": hours_from_now(24),
        },
        workspace,
    )
    insert(
        "company_profiles",
        {"company_name": full_name, "brand_voice": "Short, clear, professional.", "first_reply_mode": "mine"},
        workspace,
    )
    from engine import send_email

    send_email(
        workspace,
        email,
        "Verify your Revenue360s account",
        f"Hi {full_name},\n\nYour verification code is: {code}\n\nThis code expires in 24 hours.\n",
    )
    out = {"ok": True, "message": "Check your email for the verification code."}
    if env_flag("R360_DEV_RETURN_CODES"):  # local development only
        out["verification_code"] = code
    return out

@router.post("/login")
def login(payload: dict) -> dict:
    email = normalize_email(payload.get("email", ""))
    password = str(payload.get("password") or "")
    _throttle("login:" + email, limit=10)
    user = one("SELECT * FROM users WHERE email = ?", (email,))
    if not user or not check_password(password, user.get("password_hash") or ""):
        raise HTTPException(400, "Email or password is incorrect.")
    if not int(user.get("is_verified") or 0):
        raise HTTPException(403, "Verify your email with the code we sent before signing in.")
    return _session_payload(user)

def _matching_code(user: dict | None, code: str, token_field: str, expiry_field: str) -> bool:
    stored = str((user or {}).get(token_field) or "")
    expires = str((user or {}).get(expiry_field) or "")
    if not user or not stored or not code:
        return False
    return secrets.compare_digest(stored, code) and expires >= now_iso()

@router.post("/verify")
def verify(payload: dict) -> dict:
    email = normalize_email(payload.get("email", ""))
    code = str(payload.get("code") or "").strip()
    if not email:
        raise HTTPException(400, "Email and code are required.")
    _throttle("verify:" + email)
    user = one("SELECT * FROM users WHERE email = ?", (email,))
    if not _matching_code(user, code, "verification_token", "verification_expires_at"):
        raise HTTPException(400, "This verification code is invalid or expired.")
    execute(
        "UPDATE users SET is_verified = 1, verification_token = '', verification_expires_at = '' WHERE id = ?",
        (user["id"],),
    )
    return {"ok": True}

@router.post("/forgot")
def forgot(payload: dict) -> dict:
    email = normalize_email(payload.get("email", ""))
    generic = {"ok": True, "message": "If that account exists, a reset code has been sent."}
    _throttle("forgot:" + email, limit=5)
    user = one("SELECT * FROM users WHERE email = ?", (email,))
    if not user:
        return generic
    code = f"{secrets.randbelow(1_000_000):06d}"
    execute(
        "UPDATE users SET reset_token = ?, reset_expires_at = ? WHERE id = ?",
        (code, hours_from_now(1), user["id"]),
    )
    from engine import send_email

    send_email(
        user["workspace_id"],
        user["email"],
        "Reset your Revenue360s password",
        f"Hi {user['full_name']},\n\nYour password reset code is: {code}\nThis code expires in 1 hour.\n",
    )
    if env_flag("R360_DEV_RETURN_CODES"):  # local development only
        return {**generic, "reset_code": code}
    return generic

@router.post("/reset")
def reset(payload: dict) -> dict:
    email = normalize_email(payload.get("email", ""))
    code = str(payload.get("code") or "").strip()
    password = str(payload.get("password") or "")
    if not email:
        raise HTTPException(400, "Email and code are required.")
    if len(password) < 8:
        raise HTTPException(400, "Password must be at least 8 characters.")
    _throttle("reset:" + email)
    user = one("SELECT * FROM users WHERE email = ?", (email,))
    if not _matching_code(user, code, "reset_token", "reset_expires_at"):
        raise HTTPException(400, "This reset code is invalid or expired.")
    execute(
        "UPDATE users SET password_hash = ?, reset_token = '', reset_expires_at = '', is_verified = 1 WHERE id = ?",
        (hash_password(password), user["id"]),
    )
    execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
    return {"ok": True}

@router.get("/google-url")
def google_url() -> dict:
    client_id = env("GOOGLE_CLIENT_ID")
    if not client_id:
        return {"url": None}
    state = secrets.token_urlsafe(20)
    params = {
        "client_id": client_id,
        "redirect_uri": env("GOOGLE_REDIRECT_URI", public_url()),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "access_type": "online",
        "prompt": "select_account",
    }
    return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params), "state": state}

@router.post("/google")
def google_login(payload: dict) -> dict:
    code = payload.get("code") or ""
    settings_client = env("GOOGLE_CLIENT_ID")
    settings_secret = env("GOOGLE_CLIENT_SECRET")
    if not settings_client or not settings_secret:
        raise HTTPException(400, "Google sign-in is not configured.")
    try:
        token_response = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "code": code,
                "client_id": settings_client,
                "client_secret": settings_secret,
                "redirect_uri": env("GOOGLE_REDIRECT_URI", public_url()),
                "grant_type": "authorization_code",
            },
            timeout=20,
        )
        token_response.raise_for_status()
        profile_response = requests.get(
            "https://www.googleapis.com/oauth2/v3/userinfo",
            headers={"Authorization": f"Bearer {token_response.json()['access_token']}"},
            timeout=20,
        )
        profile_response.raise_for_status()
        profile = profile_response.json()
    except (requests.RequestException, KeyError) as error:
        raise HTTPException(400, f"Google sign-in failed: {error}") from error
    email = normalize_email(str(profile.get("email", "")))
    if not email:
        raise HTTPException(400, "Google did not provide an email address.")
    user = one("SELECT * FROM users WHERE email = ?", (email,))
    if not user:
        workspace = new_id()
        execute(
            "INSERT INTO workspaces (id, name, created_at) VALUES (?, ?, ?)",
            (workspace, f"{profile.get('name', email)}'s workspace", now_iso()),
        )
        insert(
            "users",
            {
                "workspace_id": workspace,
                "email": email,
                "full_name": profile.get("name", email),
                "google_sub": profile.get("sub", ""),
                "is_verified": 1,
            },
            workspace,
        )
        user = one("SELECT * FROM users WHERE email = ?", (email,))
    return _session_payload(user)

from db import UPLOAD_DIR, execute, insert, new_id, one, query

auth_router = router

router = APIRouter(prefix="/api/company", tags=["company"])

@router.get("")
def get_company(user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    profile = one("SELECT * FROM company_profiles WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 1", (ws,)) or {}
    docs = query(
        "SELECT id, filename, created_at, CHAR_LENGTH(extracted_text) AS chars FROM company_documents WHERE workspace_id = ? ORDER BY created_at DESC",
        (ws,),
    )
    return {"profile": profile, "documents": docs}

@router.post("")
def save_company(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    existing = one("SELECT id FROM company_profiles WHERE workspace_id = ?", (ws,))
    values = {
        "company_name": payload.get("company_name") or "",
        "website": payload.get("website") or "",
        "industry": payload.get("industry") or "",
        "offer": payload.get("offer") or "",
        "brand_voice": payload.get("brand_voice") or "",
        "first_reply_mode": payload.get("first_reply_mode") or "mine",
    }
    if existing:
        execute(
            "UPDATE company_profiles SET company_name=?, website=?, industry=?, offer=?, brand_voice=?, first_reply_mode=? WHERE id=?",
            (*values.values(), existing["id"]),
        )
    else:
        insert("company_profiles", values, ws)
    return {"ok": True}

@router.post("/documents")
async def upload_doc(file: UploadFile = File(...), user: dict = Depends(user_from_header)) -> dict:
    ws = user["workspace_id"]
    data = await file.read()
    if len(data) > 8_000_000:
        raise HTTPException(400, "File too large (8MB max).")
    stored = UPLOAD_DIR / ws
    stored.mkdir(parents=True, exist_ok=True)
    path = stored / f"{new_id()}_{file.filename}"
    path.write_bytes(data)
    from engine import extract_text

    text = extract_text(file.filename or "file.txt", data)
    insert("company_documents", {"filename": file.filename or "file", "stored_path": str(path), "extracted_text": text}, ws)
    return {"filename": file.filename, "chars": len(text)}


company_router = router