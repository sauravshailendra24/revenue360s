from __future__ import annotations
import base64
import hashlib
import json
import os
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pymysql
from pymysql.cursors import DictCursor
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

UPLOAD_DIR = ROOT / "uploads"
DEFAULT_ORG = "default-org"
ENC_PREFIX = "enc:v1:"
SECRET_FIELDS = {
    "email_connections": {"secret"},
    "sms_connections": {"secret"},
    "whatsapp_connections": {"access_token"},
    "social_accounts": {"access_token"},
    "meta_ad_accounts": {"access_token"},
    "ad_network_accounts": {"credentials"},
    "settings_kv": set(),
}
LEAD_STAGES = ("new", "contacted", "replied", "booked", "won", "lost", "unsubscribed")
PLATFORMS = ("instagram", "facebook", "threads", "tiktok", "youtube", "linkedin", "x")

def env(name: str, default: str = "") -> str:
    return os.getenv(name, default) or default

def env_flag(name: str) -> bool:
    return env(name, "").strip().lower() in {"1", "true", "yes", "on"}

def required_env(name: str) -> str:
    value = env(name, "")
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value

def db_config() -> dict:
    return {
        "host": env("DB_HOST", "127.0.0.1"),
        "port": int(env("DB_PORT", "3306") or 3306),
        "user": env("DB_USER", "root"),
        "password": required_env("DB_PASSWORD"),
        "database": env("DB_NAME", "revenue360"),
        "charset": "utf8mb4",
        "autocommit": False,
        "cursorclass": DictCursor,
    }

def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def hours_from_now(hours: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=float(hours or 0))).replace(microsecond=0).isoformat()

def new_id() -> str:
    return str(uuid.uuid4())

def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()

def normalize_email(value: str) -> str:
    return (value or "").strip().lower()

def normalize_phone(value: str) -> str:
    digits = "".join(ch for ch in (value or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits

def json_text(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)

def parse_tags(raw: object) -> list[str]:
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        loaded = json.loads(text)
        if isinstance(loaded, list):
            return [str(item).strip() for item in loaded if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in text.split(",") if part.strip()]

def format_tags(raw: object) -> str:
    return ", ".join(parse_tags(raw))

def _fernet():
    key = env("R360_SECRET_KEY").strip()
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        return None
    try:
        return Fernet(key.encode())
    except Exception:
        digest = hashlib.sha256(key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))

def require_encryption() -> None:
    if _fernet() is None:
        raise RuntimeError("R360_SECRET_KEY is required. Refusing to start with plaintext credential storage.")


def encrypt_secret(value: str) -> str:
    if not value or str(value).startswith(ENC_PREFIX):
        return value or ""
    box = _fernet()
    if not box:
        raise RuntimeError("R360_SECRET_KEY is required. Refusing to store a credential in plaintext.")
    return ENC_PREFIX + box.encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    if not value:
        return ""
    if not str(value).startswith(ENC_PREFIX):
        return value
    box = _fernet()
    if not box:
        raise RuntimeError("R360_SECRET_KEY is required to read encrypted credentials.")
    try:
        return box.decrypt(value[len(ENC_PREFIX):].encode()).decode()
    except Exception:
        return ""

def secrets_locked() -> bool:
    return _fernet() is not None

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"

def check_password(password: str, stored: str) -> bool:
    if not stored:
        return False
    try:
        algorithm, salt_hex, digest_hex = stored.split("$", 2)
        if algorithm != "pbkdf2_sha256":
            return False
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 240_000)
        return secrets.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError, AttributeError):
        return False

def _adapt(sql: str) -> str:
    return sql.replace("?", "%s")

def _connect(with_db: bool = True):
    cfg = db_config()
    if not with_db:
        cfg = {k: v for k, v in cfg.items() if k != "database"}
        cfg["cursorclass"] = DictCursor
        cfg["autocommit"] = False
    return pymysql.connect(**cfg)

@contextmanager
def db():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    connection = _connect(True)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def init_db() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    cfg = db_config()
    name = cfg["database"]
    bootstrap = _connect(False)
    try:
        with bootstrap.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        bootstrap.commit()
    finally:
        bootstrap.close()

    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    statements = [part.strip() for part in schema.split(";") if part.strip()]
    with db() as connection:
        with connection.cursor() as cur:
            for statement in statements:
                cur.execute(statement)
            account_columns = {
                "social_accounts": {"name": "VARCHAR(255) NOT NULL DEFAULT ''", "settings": "LONGTEXT"},
                "whatsapp_connections": {"name": "VARCHAR(255) NOT NULL DEFAULT ''", "settings": "LONGTEXT", "updated_at": "VARCHAR(40) NOT NULL DEFAULT ''"},
                "email_connections": {"name": "VARCHAR(255) NOT NULL DEFAULT ''", "settings": "LONGTEXT", "updated_at": "VARCHAR(40) NOT NULL DEFAULT ''"},
                "sms_connections": {"name": "VARCHAR(255) NOT NULL DEFAULT ''", "settings": "LONGTEXT", "updated_at": "VARCHAR(40) NOT NULL DEFAULT ''"},
                "meta_ad_accounts": {"name": "VARCHAR(255) NOT NULL DEFAULT ''", "settings": "LONGTEXT", "updated_at": "VARCHAR(40) NOT NULL DEFAULT ''"},
                "messages": {
                    "account_id": "VARCHAR(36) NOT NULL DEFAULT ''",
                    "campaign_id": "VARCHAR(36) NOT NULL DEFAULT ''",
                    "intent": "VARCHAR(20) NOT NULL DEFAULT ''",
                },
                "sequence_steps": {"account_id": "VARCHAR(36) NOT NULL DEFAULT ''", "variants": "LONGTEXT"},
                "scheduled_posts": {"account_ids": "LONGTEXT"},
                "ad_optimization_rules": {"conversion_limit_enabled": "TINYINT NOT NULL DEFAULT 1"},
            }
            for table, columns in account_columns.items():
                for column, definition in columns.items():
                    cur.execute(f"SHOW COLUMNS FROM {table} LIKE %s", (column,))
                    if not cur.fetchone():
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
            cur.execute("SHOW COLUMNS FROM scheduled_posts LIKE 'media'")
            media_column = cur.fetchone()
            if media_column and str(media_column.get("Type") or "").lower().startswith("varchar("):
                media_type = str(media_column.get("Type") or "").lower()
                if media_type != "varchar(2000)":
                    cur.execute("ALTER TABLE scheduled_posts MODIFY COLUMN media VARCHAR(2000) NOT NULL DEFAULT ''")
            cur.execute("SHOW COLUMNS FROM leads LIKE 'custom_fields'")
            if not cur.fetchone():
                cur.execute("ALTER TABLE leads ADD COLUMN custom_fields LONGTEXT")
            cur.execute(
                "CREATE TABLE IF NOT EXISTS sessions ("
                "token_hash VARCHAR(64) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, workspace_id VARCHAR(36) NOT NULL, "
                "email VARCHAR(255) NOT NULL, full_name VARCHAR(255) NOT NULL DEFAULT '', "
                "expires_at VARCHAR(40) NOT NULL, created_at VARCHAR(40) NOT NULL, INDEX idx_sessions_exp (expires_at))"
            )
            cur.execute(
                "INSERT IGNORE INTO workspaces (id, name, created_at) VALUES (%s, %s, %s)",
                (DEFAULT_ORG, env("R360_BRAND_NAME", "Revenue360s"), now_iso()),
            )
            cur.execute(
                "CREATE TABLE IF NOT EXISTS agent_runs ("
                "id VARCHAR(36) PRIMARY KEY,"
                "workspace_id VARCHAR(36) NOT NULL,"
                "title VARCHAR(255) NOT NULL DEFAULT '',"
                "phase VARCHAR(40) NOT NULL DEFAULT 'discovery',"
                "status VARCHAR(40) NOT NULL DEFAULT 'draft',"
                "requirements LONGTEXT,"
                "conversation LONGTEXT,"
                "workflow LONGTEXT,"
                "policy LONGTEXT,"
                "autonomy LONGTEXT,"
                "missing LONGTEXT,"
                "events LONGTEXT,"
                "metrics LONGTEXT,"
                "account_ids LONGTEXT,"
                "approval_status VARCHAR(40) NOT NULL DEFAULT '',"
                "last_question LONGTEXT,"
                "updated_at VARCHAR(40) NOT NULL DEFAULT '',"
                "created_at VARCHAR(40) NOT NULL DEFAULT '',"
                "INDEX idx_agent_ws (workspace_id, updated_at)"
                ")"
            )

def query(sql: str, params: tuple = ()) -> list[dict]:
    with db() as connection:
        with connection.cursor() as cur:
            cur.execute(_adapt(sql), params)
            rows = cur.fetchall() or []
            return [dict(row) for row in rows]

def one(sql: str, params: tuple = ()) -> dict | None:
    rows = query(sql, params)
    return rows[0] if rows else None

def execute(sql: str, params: tuple = ()) -> None:
    with db() as connection:
        with connection.cursor() as cur:
            cur.execute(_adapt(sql), params)

def insert(table: str, values: dict, workspace_id: str | None = None) -> str:
    row_id = values.get("id", new_id())
    payload = {**values, "id": row_id, "created_at": values.get("created_at", now_iso())}
    if table not in {"workspaces", "users"} and "workspace_id" not in payload:
        if not workspace_id:
            raise ValueError("workspace_id required")
        payload["workspace_id"] = workspace_id
    fields = SECRET_FIELDS.get(table, set())
    for field in fields:
        if payload.get(field):
            payload[field] = encrypt_secret(str(payload[field]))
    columns = ", ".join(f"`{key}`" if key == "key" else key for key in payload)
    placeholders = ", ".join("%s" for _ in payload)
    with db() as connection:
        with connection.cursor() as cur:
            cur.execute(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})", tuple(payload.values()))
    return row_id

def upsert(table: str, values: dict, match: dict, workspace_id: str | None = None) -> str:
    match_values = dict(match)
    where = " AND ".join(f"{key} = ?" for key in match_values)
    params = list(match_values.values())
    if workspace_id is not None:
        where = f"{where} AND workspace_id = ?" if where else "workspace_id = ?"
        params.append(workspace_id)
    existing = one(
        f"SELECT id FROM {table} WHERE {where} ORDER BY created_at DESC LIMIT 1",
        tuple(params),
    )
    if not existing:
        return insert(table, values, workspace_id=workspace_id or match_values.get("workspace_id"))
    protected = dict(values)
    for field in SECRET_FIELDS.get(table, set()):
        if protected.get(field):
            protected[field] = encrypt_secret(str(protected[field]))
    assignments = ", ".join(f"`{key}` = ?" if key == "key" else f"{key} = ?" for key in protected)
    update_sql = f"UPDATE {table} SET {assignments} WHERE id = ?"
    if workspace_id is not None or "workspace_id" in match_values:
        update_sql += " AND workspace_id = ?"
        execute(update_sql, (*protected.values(), existing["id"], workspace_id or match_values.get("workspace_id")))
    else:
        execute(update_sql, (*protected.values(), existing["id"]))
    return existing["id"]

def reveal(table: str, row: dict | None) -> dict | None:
    if not row:
        return row
    out = dict(row)
    for field in SECRET_FIELDS.get(table, set()):
        if field in out:
            out[field] = decrypt_secret(str(out[field] or ""))
    return out

def setting(workspace_id: str, key: str, fallback: str = "") -> str:
    row = one("SELECT value FROM settings_kv WHERE workspace_id = ? AND `key` = ?", (workspace_id, key))
    value = decrypt_secret((row or {}).get("value") or "") if row else ""
    return value or env(key.upper(), fallback)

def public_url() -> str:
    return env("R360_PUBLIC_URL", env("R360_BASE_URL", "https://revenue360s.com")).rstrip("/")