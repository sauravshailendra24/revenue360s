from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import quote

import requests
from fastapi import APIRouter, Depends, HTTPException

from auth import user_from_header
from db import (
    decrypt_secret,
    encrypt_secret,
    env,
    env_flag,
    execute,
    hours_from_now,
    json_text,
    new_id,
    now_iso,
    one,
    query,
    setting,
)

CLOUD_PROVIDERS = ("aws", "gcp", "azure")
JEV_QUESTIONS_MAX = 20
APPLY_FLAG = "CLOUD_ALLOW_APPLY"

# Seed catalog is an offline estimate, never reported as a live invoice.
# Azure retail sync overwrites matching rows with source=azure_retail.
SEED_REGIONS = [
    ("aws", "ap-south-1", "Asia Pacific (Mumbai)", "IN", "AS", 19.07, 72.87),
    ("aws", "ap-southeast-1", "Asia Pacific (Singapore)", "SG", "AS", 1.35, 103.82),
    ("aws", "us-east-1", "US East (N. Virginia)", "US", "NA", 38.13, -78.45),
    ("gcp", "asia-south1", "Mumbai", "IN", "AS", 19.07, 72.87),
    ("gcp", "asia-southeast1", "Singapore", "SG", "AS", 1.35, 103.82),
    ("gcp", "us-central1", "Iowa", "US", "NA", 41.26, -95.86),
    ("azure", "centralindia", "Central India", "IN", "AS", 18.58, 73.74),
    ("azure", "southeastasia", "Southeast Asia", "SG", "AS", 1.28, 103.83),
    ("azure", "eastus", "East US", "US", "NA", 37.37, -79.97),
]

SEED_PRICES = [
    # provider, service, sku, region, unit, price, price_type, model
    ("aws", "ec2", "t3.small", "ap-south-1", "hour", 0.0216, "ondemand", "per_hour"),
    ("aws", "ec2", "t3.medium", "ap-south-1", "hour", 0.0432, "ondemand", "per_hour"),
    ("aws", "rds", "db.t3.micro", "ap-south-1", "hour", 0.018, "ondemand", "per_hour"),
    ("aws", "rds", "db.t3.small", "ap-south-1", "hour", 0.036, "ondemand", "per_hour"),
    ("gcp", "cloud_run", "request-cpu", "asia-south1", "vCPU-second", 0.000024, "ondemand", "usage"),
    ("gcp", "cloud_sql", "db-custom-1-3840", "asia-south1", "hour", 0.0645, "ondemand", "per_hour"),
    ("azure", "container_apps", "consumption-vcpu", "centralindia", "vCPU-second", 0.000024, "Consumption", "usage"),
    ("azure", "azure_database_postgresql", "B1ms", "centralindia", "hour", 0.016, "Consumption", "per_hour"),
]

ARCHITECTURES = [
    {
        "name": "fastapi_cloudrun_postgres",
        "category": "web_api",
        "provider": "gcp",
        "components": [
            {"role": "app", "service": "cloud_run", "sku": "request-cpu"},
            {"role": "database", "service": "cloud_sql", "sku": "db-custom-1-3840", "engine": "postgresql"},
            {"role": "object", "service": "gcs", "sku": "standard"},
        ],
        "requirements": {"containerized": True, "database": "postgresql", "managed": True},
        "security": {"public_database": False, "private_network": True, "tls": True, "ssh_open": False, "backups": True},
        "ops_burden": 0.2,
    },
    {
        "name": "fastapi_ecs_rds",
        "category": "web_api",
        "provider": "aws",
        "components": [
            {"role": "app", "service": "ecs", "sku": "t3.small"},
            {"role": "database", "service": "rds", "sku": "db.t3.micro", "engine": "postgresql"},
            {"role": "object", "service": "s3", "sku": "standard"},
        ],
        "requirements": {"containerized": True, "database": "postgresql", "managed": True},
        "security": {"public_database": False, "private_network": True, "tls": True, "ssh_open": False, "backups": True},
        "ops_burden": 0.45,
    },
    {
        "name": "fastapi_ec2_rds",
        "category": "web_api",
        "provider": "aws",
        "components": [
            {"role": "app", "service": "ec2", "sku": "t3.small"},
            {"role": "database", "service": "rds", "sku": "db.t3.micro", "engine": "postgresql"},
        ],
        "requirements": {"containerized": False, "database": "postgresql", "managed": False},
        "security": {"public_database": False, "private_network": True, "tls": True, "ssh_open": True, "backups": True},
        "ops_burden": 0.7,
    },
    {
        "name": "fastapi_containerapps_postgres",
        "category": "web_api",
        "provider": "azure",
        "components": [
            {"role": "app", "service": "container_apps", "sku": "consumption-vcpu"},
            {"role": "database", "service": "azure_database_postgresql", "sku": "B1ms", "engine": "postgresql"},
        ],
        "requirements": {"containerized": True, "database": "postgresql", "managed": True},
        "security": {"public_database": False, "private_network": True, "tls": True, "ssh_open": False, "backups": True},
        "ops_burden": 0.25,
    },
]

COMPAT = [
    ("fastapi", "cloud_run", "native", 0.95, "Container service. No VM to patch."),
    ("fastapi", "ecs", "native", 0.85, "Fargate/ECS runs the image. More networking setup than Cloud Run."),
    ("fastapi", "container_apps", "native", 0.9, "Container Apps runs the image. Consumption plan fits low traffic."),
    ("fastapi", "lambda", "adapt", 0.4, "Needs a Lambda adapter and cold-start budget. Not the default."),
    ("postgresql", "cloud_sql", "native", 0.95, "Managed PostgreSQL."),
    ("postgresql", "rds", "native", 0.95, "Managed PostgreSQL."),
    ("postgresql", "azure_database_postgresql", "native", 0.9, "Flexible server."),
]

QUESTION_BANK = {
    "region": {"prompt": "Which region should the primary deployment use?", "options": ["india", "singapore", "us"]},
    "database": {"prompt": "Which database, and do you already run it?", "options": ["postgresql_new", "postgresql_existing", "mysql_new", "none"]},
    "traffic": {"prompt": "What is the peak request rate?", "options": ["5 rps", "30 rps", "100 rps"]},
    "budget": {"prompt": "What is the monthly ceiling in USD?", "options": ["25", "50", "100", "250"]},
    "availability": {"prompt": "What availability do you actually need?", "options": ["99.5", "99.9", "99.99"]},
}

JOB_HOURS = {
    "azure_prices": 1,
    "aws_services": 12,
    "gcp_services": 12,
    "operations": 5 / 60,
    "health": 10 / 60,
}


def _load(value, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, json.JSONDecodeError):
        return default


def _table(sql: str) -> None:
    execute(sql)


def ensure_cloud_tables() -> None:
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_providers ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, name VARCHAR(80) NOT NULL, "
        "api_version VARCHAR(40) NOT NULL DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'active', "
        "last_sync_at VARCHAR(40) NOT NULL DEFAULT '', metadata_json LONGTEXT, UNIQUE KEY uq_cloud_provider (provider))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_services ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, service_code VARCHAR(80) NOT NULL, "
        "service_name VARCHAR(160) NOT NULL DEFAULT '', category VARCHAR(40) NOT NULL DEFAULT '', "
        "description VARCHAR(500) NOT NULL DEFAULT '', capabilities_json LONGTEXT, status VARCHAR(20) NOT NULL DEFAULT 'active', "
        "last_synced_at VARCHAR(40) NOT NULL DEFAULT '', UNIQUE KEY uq_cloud_service (provider, service_code))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_regions ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, region_code VARCHAR(40) NOT NULL, "
        "region_name VARCHAR(120) NOT NULL DEFAULT '', country VARCHAR(8) NOT NULL DEFAULT '', continent VARCHAR(8) NOT NULL DEFAULT '', "
        "latitude DOUBLE NULL, longitude DOUBLE NULL, availability VARCHAR(20) NOT NULL DEFAULT 'available', "
        "metadata_json LONGTEXT, last_synced_at VARCHAR(40) NOT NULL DEFAULT '', UNIQUE KEY uq_cloud_region (provider, region_code))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_skus ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, service VARCHAR(80) NOT NULL, sku VARCHAR(120) NOT NULL, "
        "sku_name VARCHAR(160) NOT NULL DEFAULT '', family VARCHAR(80) NOT NULL DEFAULT '', category VARCHAR(40) NOT NULL DEFAULT '', "
        "cpu DOUBLE NULL, memory_gb DOUBLE NULL, gpu TINYINT NOT NULL DEFAULT 0, architecture VARCHAR(40) NOT NULL DEFAULT '', "
        "region VARCHAR(40) NOT NULL DEFAULT '', metadata_json LONGTEXT, last_synced_at VARCHAR(40) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_cloud_sku (provider, service, sku, region))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_prices ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, service VARCHAR(80) NOT NULL, sku VARCHAR(120) NOT NULL, "
        "region VARCHAR(40) NOT NULL DEFAULT '', currency VARCHAR(8) NOT NULL DEFAULT 'USD', unit VARCHAR(40) NOT NULL, "
        "unit_quantity DOUBLE NOT NULL DEFAULT 1, price DOUBLE NOT NULL, price_type VARCHAR(40) NOT NULL DEFAULT 'ondemand', "
        "pricing_model VARCHAR(40) NOT NULL DEFAULT '', source VARCHAR(40) NOT NULL DEFAULT 'seed', raw_json LONGTEXT, "
        "last_synced_at VARCHAR(40) NOT NULL DEFAULT '', UNIQUE KEY uq_cloud_price (provider, service, sku, region, unit, price_type))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_capabilities ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL, service VARCHAR(80) NOT NULL, capability VARCHAR(80) NOT NULL, "
        "resource_type VARCHAR(80) NOT NULL DEFAULT '', operation VARCHAR(40) NOT NULL DEFAULT '', supported TINYINT NOT NULL DEFAULT 1, "
        "regions_json LONGTEXT, constraints_json LONGTEXT, last_synced_at VARCHAR(40) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_cloud_cap (provider, service, capability))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_architectures ("
        "id VARCHAR(36) PRIMARY KEY, name VARCHAR(80) NOT NULL, category VARCHAR(40) NOT NULL DEFAULT '', provider VARCHAR(20) NOT NULL, "
        "components_json LONGTEXT, requirements_json LONGTEXT, security_profile_json LONGTEXT, cost_formula_json LONGTEXT, "
        "terraform_template LONGTEXT, version INT NOT NULL DEFAULT 1, status VARCHAR(20) NOT NULL DEFAULT 'active', "
        "UNIQUE KEY uq_cloud_arch (name))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_compatibility ("
        "id VARCHAR(36) PRIMARY KEY, source VARCHAR(80) NOT NULL, target VARCHAR(80) NOT NULL, compatibility VARCHAR(20) NOT NULL, "
        "score DOUBLE NOT NULL DEFAULT 0, notes VARCHAR(400) NOT NULL DEFAULT '', UNIQUE KEY uq_cloud_compat (source, target))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_accounts ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, provider VARCHAR(20) NOT NULL, "
        "account_name VARCHAR(160) NOT NULL DEFAULT '', account_identifier VARCHAR(160) NOT NULL DEFAULT '', "
        "credential_type VARCHAR(40) NOT NULL DEFAULT '', credentials_encrypted LONGTEXT, status VARCHAR(20) NOT NULL DEFAULT 'pending', "
        "permissions_json LONGTEXT, regions_json LONGTEXT, last_verified_at VARCHAR(40) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL DEFAULT '', updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_cloud_accounts_ws (workspace_id, provider))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_plans ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, provider VARCHAR(20) NOT NULL DEFAULT '', "
        "architecture_id VARCHAR(80) NOT NULL DEFAULT '', requirements_json LONGTEXT, architecture_json LONGTEXT, "
        "cost_json LONGTEXT, security_json LONGTEXT, terraform_json LONGTEXT, deployment_json LONGTEXT, "
        "jev_decision_json LONGTEXT, ai_explanation LONGTEXT, confidence DOUBLE NOT NULL DEFAULT 0, "
        "status VARCHAR(40) NOT NULL DEFAULT 'draft', approval_status VARCHAR(40) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL DEFAULT '', updated_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_cloud_plans_ws (workspace_id, updated_at))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_resources ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, cloud_account_id VARCHAR(36) NOT NULL DEFAULT '', "
        "provider VARCHAR(20) NOT NULL, resource_id VARCHAR(200) NOT NULL DEFAULT '', resource_type VARCHAR(80) NOT NULL DEFAULT '', "
        "resource_name VARCHAR(160) NOT NULL DEFAULT '', region VARCHAR(40) NOT NULL DEFAULT '', status VARCHAR(40) NOT NULL DEFAULT '', "
        "configuration_json LONGTEXT, tags_json LONGTEXT, cost_estimate DOUBLE NULL, actual_cost DOUBLE NULL, "
        "health_status VARCHAR(40) NOT NULL DEFAULT '', last_seen_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_cloud_resources_ws (workspace_id, provider))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_operations ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, plan_id VARCHAR(36) NOT NULL DEFAULT '', "
        "provider VARCHAR(20) NOT NULL DEFAULT '', operation_type VARCHAR(40) NOT NULL, resource_id VARCHAR(200) NOT NULL DEFAULT '', "
        "request_json LONGTEXT, response_json LONGTEXT, status VARCHAR(40) NOT NULL DEFAULT 'pending', "
        "provider_operation_id VARCHAR(120) NOT NULL DEFAULT '', error_code VARCHAR(80) NOT NULL DEFAULT '', "
        "error_message VARCHAR(500) NOT NULL DEFAULT '', started_at VARCHAR(40) NOT NULL DEFAULT '', completed_at VARCHAR(40) NOT NULL DEFAULT '', "
        "INDEX idx_cloud_ops_ws (workspace_id, status))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_metrics ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, resource_id VARCHAR(36) NOT NULL, "
        "provider VARCHAR(20) NOT NULL, metric VARCHAR(80) NOT NULL, timestamp VARCHAR(40) NOT NULL, "
        "value DOUBLE NOT NULL, unit VARCHAR(40) NOT NULL DEFAULT '', dimensions_json LONGTEXT, "
        "INDEX idx_cloud_metrics_res (resource_id, timestamp))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_feedback ("
        "id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, plan_id VARCHAR(36) NOT NULL DEFAULT '', "
        "feedback_type VARCHAR(40) NOT NULL, expected VARCHAR(160) NOT NULL DEFAULT '', actual VARCHAR(160) NOT NULL DEFAULT '', "
        "rating INT NULL, reason VARCHAR(240) NOT NULL DEFAULT '', user_comment VARCHAR(1000) NOT NULL DEFAULT '', "
        "created_at VARCHAR(40) NOT NULL DEFAULT '', INDEX idx_cloud_feedback_ws (workspace_id, plan_id))"
    )
    _table(
        "CREATE TABLE IF NOT EXISTS cloud_sync_jobs ("
        "id VARCHAR(36) PRIMARY KEY, provider VARCHAR(20) NOT NULL DEFAULT '', job_type VARCHAR(40) NOT NULL, "
        "scope VARCHAR(80) NOT NULL DEFAULT '', status VARCHAR(20) NOT NULL DEFAULT 'idle', "
        "last_run_at VARCHAR(40) NOT NULL DEFAULT '', next_run_at VARCHAR(40) NOT NULL DEFAULT '', "
        "records_processed INT NOT NULL DEFAULT 0, records_changed INT NOT NULL DEFAULT 0, "
        "error_count INT NOT NULL DEFAULT 0, error_message VARCHAR(500) NOT NULL DEFAULT '', "
        "UNIQUE KEY uq_cloud_job (provider, job_type, scope))"
    )
    _seed_catalog()
    _schedule_jobs()


def _upsert_simple(table: str, keys: dict, values: dict) -> None:
    where = " AND ".join(f"{key} = ?" for key in keys)
    existing = one(f"SELECT id FROM {table} WHERE {where} LIMIT 1", tuple(keys.values()))
    payload = {**keys, **values}
    if existing:
        assignments = ", ".join(f"{key} = ?" for key in values)
        execute(f"UPDATE {table} SET {assignments} WHERE id = ?", (*values.values(), existing["id"]))
        return
    payload["id"] = new_id()
    columns = ", ".join(payload)
    execute(
        f"INSERT INTO {table} ({columns}) VALUES ({', '.join('?' for _ in payload)})",
        tuple(payload.values()),
    )


def _seed_catalog() -> None:
    for provider, code, name, country, continent, lat, lon in SEED_REGIONS:
        _upsert_simple(
            "cloud_regions",
            {"provider": provider, "region_code": code},
            {"region_name": name, "country": country, "continent": continent, "latitude": lat, "longitude": lon, "last_synced_at": now_iso()},
        )
    for provider, service, sku, region, unit, price, price_type, model in SEED_PRICES:
        existing = one(
            "SELECT id, source FROM cloud_prices WHERE provider = ? AND service = ? AND sku = ? AND region = ? AND unit = ? AND price_type = ?",
            (provider, service, sku, region, unit, price_type),
        )
        if existing and existing.get("source") not in {"", "seed"}:
            continue
        _upsert_simple(
            "cloud_prices",
            {"provider": provider, "service": service, "sku": sku, "region": region, "unit": unit, "price_type": price_type},
            {"currency": "USD", "unit_quantity": 1, "price": price, "pricing_model": model, "source": "seed", "last_synced_at": now_iso()},
        )
    for arch in ARCHITECTURES:
        _upsert_simple(
            "cloud_architectures",
            {"name": arch["name"]},
            {
                "category": arch["category"],
                "provider": arch["provider"],
                "components_json": json_text(arch["components"]),
                "requirements_json": json_text(arch["requirements"]),
                "security_profile_json": json_text(arch["security"]),
                "cost_formula_json": json_text({"ops_burden": arch["ops_burden"]}),
                "status": "active",
            },
        )
    for source, target, compatibility, score, notes in COMPAT:
        _upsert_simple(
            "cloud_compatibility",
            {"source": source, "target": target},
            {"compatibility": compatibility, "score": score, "notes": notes},
        )
    for provider, name in (("aws", "Amazon Web Services"), ("gcp", "Google Cloud"), ("azure", "Microsoft Azure")):
        _upsert_simple("cloud_providers", {"provider": provider}, {"name": name, "status": "active", "api_version": "v1"})


def _schedule_jobs() -> None:
    jobs = [
        ("azure", "azure_prices", "centralindia"),
        ("aws", "aws_services", "public-index"),
        ("gcp", "gcp_services", "catalog"),
        ("", "operations", "active"),
        ("", "health", "active"),
    ]
    for provider, job_type, scope in jobs:
        existing = one(
            "SELECT id FROM cloud_sync_jobs WHERE provider = ? AND job_type = ? AND scope = ?",
            (provider, job_type, scope),
        )
        if existing:
            continue
        execute(
            "INSERT INTO cloud_sync_jobs (id, provider, job_type, scope, status, next_run_at) VALUES (?, ?, ?, ?, 'idle', ?)",
            (new_id(), provider, job_type, scope, now_iso()),
        )


def _region_for(provider: str, preference: str) -> str:
    preference = (preference or "").lower()
    mapping = {
        "aws": {"india": "ap-south-1", "singapore": "ap-southeast-1", "us": "us-east-1"},
        "gcp": {"india": "asia-south1", "singapore": "asia-southeast1", "us": "us-central1"},
        "azure": {"india": "centralindia", "singapore": "southeastasia", "us": "eastus"},
    }
    return mapping.get(provider, {}).get(preference) or mapping.get(provider, {}).get("india")


def _extract_requirements(text: str, current: dict | None, overrides: dict | None) -> dict:
    req = {
        "application": {"framework": None, "language": None, "runtime": None},
        "database": {"type": None, "existing": False},
        "traffic": {"requests_per_day": None, "peak_rps": None},
        "region": {"primary": None, "preferred": None},
        "availability": {"target": None},
        "budget": {"monthly_max": None},
        "preferences": {"managed_services": True, "containerized": None, "avoid_kubernetes": False, "provider": None},
        "security": {"public_database": False, "private_network": True},
    }
    if isinstance(current, dict):
        for key, value in current.items():
            if isinstance(value, dict) and isinstance(req.get(key), dict):
                req[key].update(value)
            else:
                req[key] = value
    if isinstance(overrides, dict):
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(req.get(key), dict):
                req[key].update(value)
            else:
                req[key] = value
    lower = (text or "").lower()
    if "fastapi" in lower or "fast api" in lower:
        req["application"]["framework"] = "fastapi"
        req["application"]["language"] = "python"
    elif "django" in lower:
        req["application"]["framework"] = "django"
        req["application"]["language"] = "python"
    elif "node" in lower:
        req["application"]["framework"] = "node"
        req["application"]["language"] = "javascript"
    if "postgres" in lower or "postgresql" in lower:
        req["database"]["type"] = "postgresql"
    elif "mysql" in lower:
        req["database"]["type"] = "mysql"
    if any(phrase in lower for phrase in ("existing database", "existing postgres", "already have a database", "already have postgres")):
        req["database"]["existing"] = True
    if "india" in lower or "mumbai" in lower:
        req["region"]["primary"] = "india"
    elif "singapore" in lower:
        req["region"]["primary"] = "singapore"
    if "ec2" in lower or "everything on ec2" in lower:
        req["preferences"]["managed_services"] = False
        req["preferences"]["containerized"] = False
        req["preferences"]["provider"] = "aws"
    if "docker" in lower or "container" in lower:
        req["preferences"]["containerized"] = True
    if "don't care about cost" in lower or "maximum reliability" in lower:
        req["budget"]["monthly_max"] = req["budget"].get("monthly_max") or 500
        req["availability"]["target"] = "99.99"
    budget = re.search(r"\$?\s*(\d{2,5})\s*(?:/|\s)?\s*(?:month|mo|usd)?", lower)
    if budget and ("$" in lower or "usd" in lower or "budget" in lower or "under" in lower):
        req["budget"]["monthly_max"] = int(budget.group(1))
    rps = re.search(r"(\d+)\s*(?:rps|requests/sec|req/s)", lower)
    if rps:
        req["traffic"]["peak_rps"] = int(rps.group(1))
    users = re.search(r"(\d+)\s*k\s*users", lower)
    if users and not req["traffic"].get("requests_per_day"):
        req["traffic"]["requests_per_day"] = int(users.group(1)) * 1000
    avail = re.search(r"(99(?:\.\d+)?)", lower)
    if avail:
        req["availability"]["target"] = avail.group(1)
    if "aws" in lower:
        req["preferences"]["provider"] = "aws"
    elif "gcp" in lower or "google cloud" in lower:
        req["preferences"]["provider"] = "gcp"
    elif "azure" in lower:
        req["preferences"]["provider"] = "azure"
    return req


def _missing(req: dict) -> list[str]:
    missing = []
    if not (req.get("region") or {}).get("primary"):
        missing.append("region")
    if not (req.get("database") or {}).get("type"):
        missing.append("database")
    if (req.get("traffic") or {}).get("peak_rps") in (None, ""):
        missing.append("traffic")
    if (req.get("budget") or {}).get("monthly_max") in (None, ""):
        missing.append("budget")
    if not (req.get("availability") or {}).get("target"):
        missing.append("availability")
    return missing


def _apply_answer(req: dict, field: str, value: str) -> dict:
    value = str(value or "").strip()
    if field == "region":
        req.setdefault("region", {})["primary"] = value.lower()
    elif field == "database":
        req.setdefault("database", {})
        if value.startswith("postgresql"):
            req["database"]["type"] = "postgresql"
        elif value.startswith("mysql"):
            req["database"]["type"] = "mysql"
        else:
            req["database"]["type"] = "none"
        req["database"]["existing"] = value.endswith("existing")
    elif field == "traffic":
        match = re.search(r"(\d+)", value)
        req.setdefault("traffic", {})["peak_rps"] = int(match.group(1)) if match else 5
    elif field == "budget":
        match = re.search(r"(\d+)", value)
        req.setdefault("budget", {})["monthly_max"] = int(match.group(1)) if match else 50
    elif field == "availability":
        req.setdefault("availability", {})["target"] = value
    return req


def _price_row(provider: str, service: str, sku: str, region: str) -> dict | None:
    return one(
        "SELECT * FROM cloud_prices WHERE provider = ? AND service = ? AND sku = ? AND region = ? ORDER BY last_synced_at DESC LIMIT 1",
        (provider, service, sku, region),
    )


def _monthly(row: dict | None, peak_rps: int) -> tuple[float, str]:
    if not row:
        return 0.0, "missing"
    price = float(row.get("price") or 0)
    model = row.get("pricing_model") or ""
    unit = str(row.get("unit") or "").lower()
    source = row.get("source") or "seed"
    if model == "per_hour" or "hour" in unit:
        return round(price * 730, 2), source
    # Usage SKUs: idle-low estimate from peak. Not an invoice.
    seconds = max(peak_rps, 1) * 0.05 * 86400 * 30
    return round(price * seconds, 2), source


def _candidates(req: dict) -> list[dict]:
    preferred = (req.get("preferences") or {}).get("provider")
    managed = (req.get("preferences") or {}).get("managed_services", True)
    containerized = (req.get("preferences") or {}).get("containerized")
    peak = int((req.get("traffic") or {}).get("peak_rps") or 5)
    budget = float((req.get("budget") or {}).get("monthly_max") or 0)
    region_pref = (req.get("region") or {}).get("primary") or "india"
    existing_db = bool((req.get("database") or {}).get("existing"))
    out = []
    for arch in ARCHITECTURES:
        if preferred and arch["provider"] != preferred and preferred in CLOUD_PROVIDERS:
            continue
        if managed is False and arch["requirements"].get("managed"):
            continue
        if containerized is True and not arch["requirements"].get("containerized"):
            continue
        if containerized is False and arch["requirements"].get("containerized"):
            continue
        region = _region_for(arch["provider"], region_pref)
        components = []
        total = 0.0
        sources = set()
        for component in arch["components"]:
            if existing_db and component.get("role") == "database":
                components.append({**component, "monthly": 0, "source": "existing", "region": region})
                continue
            row = _price_row(arch["provider"], component["service"], component["sku"], region)
            monthly, source = _monthly(row, peak)
            sources.add(source)
            total += monthly
            components.append({**component, "monthly": monthly, "source": source, "region": region})
        security = _security(arch["security"], req)
        cost_fit = 1.0
        if budget and total > budget:
            cost_fit = max(0.1, budget / total)
        elif budget and total:
            cost_fit = min(1.0, budget / max(total, 1))
        score = round((0.35 * (1 - arch["ops_burden"])) + (0.25 * cost_fit) + (0.2 * security["score"]) + (0.2 * (1 - arch["ops_burden"])), 4)
        out.append({
            "name": arch["name"],
            "provider": arch["provider"],
            "region": region,
            "components": components,
            "monthly_estimate": round(total, 2),
            "price_sources": sorted(sources),
            "security": security,
            "ops_burden": arch["ops_burden"],
            "score": score,
            "within_budget": (not budget) or total <= budget,
        })
    return sorted(out, key=lambda item: item["score"], reverse=True)


def _security(profile: dict, req: dict) -> dict:
    findings = []
    if profile.get("public_database") or (req.get("security") or {}).get("public_database"):
        findings.append({"severity": "high", "code": "public_database", "evidence": "Database is marked public.", "automatic_fix_available": True})
    if profile.get("ssh_open"):
        findings.append({"severity": "high", "code": "ssh_open", "evidence": "VM architecture needs SSH. Default must not be 0.0.0.0/0.", "automatic_fix_available": True})
    if not profile.get("tls"):
        findings.append({"severity": "medium", "code": "missing_tls", "evidence": "TLS is not in the architecture profile.", "automatic_fix_available": True})
    if not profile.get("backups"):
        findings.append({"severity": "medium", "code": "missing_backups", "evidence": "Backups are not in the architecture profile.", "automatic_fix_available": True})
    high = sum(1 for item in findings if item["severity"] == "high")
    score = 1.0 if not findings else max(0.2, 1 - (0.35 * high) - (0.1 * (len(findings) - high)))
    return {"score": round(score, 4), "findings": findings, "requires_approval": bool(findings)}


def _jev(state: dict, candidates: list[dict]) -> dict:
    key = env("JEV_API_KEY") or env("TYPESAFE_API_KEY") or env("JEVMODEL_API_KEY")
    base = env("JEV_BASE_URL", "https://api.typesafe.ai").rstrip("/")
    if not key or not candidates:
        return {"provider": "deterministic", "choice": candidates[0]["name"] if candidates else "", "confidence": candidates[0]["score"] if candidates else 0}
    criteria = {item["name"]: f"{item['provider']} {item['region']} est ${item['monthly_estimate']} sources {','.join(item['price_sources'])}" for item in candidates[:8]}
    body = {
        "model": env("JEV_MODEL", "jev-latest"),
        "state": state,
        "questions": {
            "architecture": {"type": "choice", "instructions": "Which architecture best satisfies the requirements and budget?", "criteria": criteria},
            "needs_human": {"type": "noul", "instructions": "Does this recommendation need another user clarification before approval?"},
        },
    }
    try:
        response = requests.post(
            f"{base}/v1/systemone",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        answers = payload.get("answers") or {}
        choice = ((answers.get("architecture") or {}).get("choice")) or candidates[0]["name"]
        confidence = float((answers.get("architecture") or {}).get("confidence") or 0)
        if choice not in criteria:
            choice = candidates[0]["name"]
            confidence = candidates[0]["score"]
        return {"provider": "jev", "model": payload.get("model") or body["model"], "choice": choice, "confidence": confidence, "answers": answers}
    except (requests.RequestException, ValueError, TypeError) as error:
        return {"provider": "deterministic", "choice": candidates[0]["name"], "confidence": candidates[0]["score"], "error": str(error)[:200]}


def _explain(workspace_id: str, req: dict, chosen: dict, decision: dict) -> str:
    facts = (
        f"Provider {chosen['provider']}, region {chosen['region']}, estimate ${chosen['monthly_estimate']} USD/month. "
        f"Price sources: {', '.join(chosen['price_sources']) or 'none'}. "
        f"Decision source: {decision.get('provider')}. Security findings: {len(chosen['security']['findings'])}."
    )
    key = setting(workspace_id, "deepseek_api_key", env("DEEPSEEK_API_KEY"))
    if not key:
        return facts + " Seed prices are estimates until a provider catalog sync replaces them. Approval is required before any write."
    try:
        response = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "deepseek-chat",
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "Explain the infrastructure recommendation in under 120 words. Use only the facts given. Do not invent prices."},
                    {"role": "user", "content": json_text({"requirements": req, "chosen": chosen, "facts": facts})},
                ],
            },
            timeout=30,
        )
        response.raise_for_status()
        return response.json()["choices"][0]["message"]["content"][:1200]
    except (requests.RequestException, KeyError, ValueError):
        return facts


def _terraform(chosen: dict, req: dict) -> str:
    provider = chosen["provider"]
    region = chosen["region"]
    lines = [
        f"# Generated plan. Not applied. Provider={provider} region={region}",
        f"# Price sources: {', '.join(chosen['price_sources'])}",
    ]
    if provider == "gcp":
        lines += [
            'provider "google" {',
            f'  region = "{region}"',
            "}",
            'resource "google_cloud_run_v2_service" "app" {',
            '  name = "revenue360-app"',
            f'  location = "{region}"',
            "}",
        ]
        if not (req.get("database") or {}).get("existing"):
            lines += ['resource "google_sql_database_instance" "db" {', '  name = "revenue360-pg"', '  database_version = "POSTGRES_15"', "}"]
    elif provider == "aws":
        lines += ['provider "aws" {', f'  region = "{region}"', "}"]
        if any(item["service"] == "ecs" for item in chosen["components"]):
            lines += ['resource "aws_ecs_cluster" "app" { name = "revenue360" }']
        else:
            lines += ['resource "aws_instance" "app" { ami = "resolve-at-apply" instance_type = "t3.small" }']
        if not (req.get("database") or {}).get("existing"):
            lines += ['resource "aws_db_instance" "db" { engine = "postgres" instance_class = "db.t3.micro" }']
    else:
        lines += ['provider "azurerm" { features {} }', f'# region {region}', 'resource "azurerm_container_app" "app" { name = "revenue360-app" }']
    return "\n".join(lines) + "\n"


def _public_plan(row: dict | None) -> dict:
    if not row:
        return {}
    return {
        "id": row["id"],
        "provider": row.get("provider") or "",
        "architecture_id": row.get("architecture_id") or "",
        "requirements": _load(row.get("requirements_json"), {}),
        "architecture": _load(row.get("architecture_json"), {}),
        "cost": _load(row.get("cost_json"), {}),
        "security": _load(row.get("security_json"), {}),
        "terraform": row.get("terraform_json") or "",
        "deployment": _load(row.get("deployment_json"), {}),
        "jev": _load(row.get("jev_decision_json"), {}),
        "explanation": row.get("ai_explanation") or "",
        "confidence": row.get("confidence") or 0,
        "status": row.get("status") or "draft",
        "approval_status": row.get("approval_status") or "",
        "updated_at": row.get("updated_at") or "",
    }


def _save_plan(workspace_id: str, plan_id: str | None, values: dict) -> str:
    values["updated_at"] = now_iso()
    if plan_id and one("SELECT id FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, workspace_id)):
        assignments = ", ".join(f"{key} = ?" for key in values)
        execute(f"UPDATE cloud_plans SET {assignments} WHERE id = ? AND workspace_id = ?", (*values.values(), plan_id, workspace_id))
        return plan_id
    plan_id = new_id()
    payload = {"id": plan_id, "workspace_id": workspace_id, "created_at": now_iso(), **values}
    columns = ", ".join(payload)
    execute(
        f"INSERT INTO cloud_plans ({columns}) VALUES ({', '.join('?' for _ in payload)})",
        tuple(payload.values()),
    )
    return plan_id


def _recommend(workspace_id: str, req: dict, plan_id: str | None) -> dict:
    missing = _missing(req)
    if missing:
        field = missing[0]
        plan_id = _save_plan(workspace_id, plan_id, {
            "provider": "",
            "architecture_id": "",
            "requirements_json": json_text(req),
            "architecture_json": json_text({}),
            "cost_json": json_text({}),
            "security_json": json_text({}),
            "terraform_json": "",
            "deployment_json": json_text({}),
            "jev_decision_json": json_text({}),
            "ai_explanation": "",
            "confidence": 0,
            "status": "awaiting_information",
            "approval_status": "",
        })
        saved = _public_plan(one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, workspace_id)))
        saved["ask"] = {"field": field, **QUESTION_BANK[field]}
        saved["missing"] = missing
        return saved
    candidates = _candidates(req)
    if not candidates:
        raise HTTPException(400, "No architecture matches these constraints.")
    decision = _jev({"requirements": req, "candidates": candidates}, candidates)
    chosen = next((item for item in candidates if item["name"] == decision.get("choice")), candidates[0])
    explanation = _explain(workspace_id, req, chosen, decision)
    plan_id = _save_plan(workspace_id, plan_id, {
        "provider": chosen["provider"],
        "architecture_id": chosen["name"],
        "requirements_json": json_text(req),
        "architecture_json": json_text({"chosen": chosen, "alternatives": candidates[1:4]}),
        "cost_json": json_text({"monthly_estimate": chosen["monthly_estimate"], "sources": chosen["price_sources"], "currency": "USD"}),
        "security_json": json_text(chosen["security"]),
        "terraform_json": _terraform(chosen, req),
        "deployment_json": json_text({"apply_enabled": env_flag(APPLY_FLAG), "steps": ["architecture", "plan", "deploy"]}),
        "jev_decision_json": json_text(decision),
        "ai_explanation": explanation,
        "confidence": float(decision.get("confidence") or chosen["score"]),
        "status": "awaiting_architecture_approval",
        "approval_status": "",
    })
    return _public_plan(one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, workspace_id)))


def _account_or_404(workspace_id: str, account_id: str) -> dict:
    row = one("SELECT * FROM cloud_accounts WHERE id = ? AND workspace_id = ?", (account_id, workspace_id))
    if not row:
        raise HTTPException(404, "Cloud account not found.")
    return row


def _creds(row: dict) -> dict:
    raw = decrypt_secret(row.get("credentials_encrypted") or "")
    loaded = _load(raw, {})
    return loaded if isinstance(loaded, dict) else {}


def _verify_account(row: dict) -> dict:
    creds = _creds(row)
    provider = row.get("provider")
    if provider == "aws":
        try:
            import boto3
        except ImportError:
            return {"ok": False, "detail": "boto3 is not installed. Credentials stored, not verified."}
        try:
            client = boto3.client(
                "sts",
                aws_access_key_id=creds.get("access_key_id") or creds.get("aws_access_key_id"),
                aws_secret_access_key=creds.get("secret_access_key") or creds.get("aws_secret_access_key"),
                aws_session_token=creds.get("session_token") or None,
                region_name=creds.get("region") or "ap-south-1",
            )
            ident = client.get_caller_identity()
            return {"ok": True, "account": ident.get("Account"), "arn": ident.get("Arn")}
        except Exception as error:
            return {"ok": False, "detail": str(error)[:240]}
    if provider == "azure":
        tenant = creds.get("tenant_id")
        client_id = creds.get("client_id")
        secret = creds.get("client_secret")
        subscription = creds.get("subscription_id")
        if not all((tenant, client_id, secret, subscription)):
            return {"ok": False, "detail": "Azure verify needs tenant_id, client_id, client_secret, subscription_id."}
        try:
            token = requests.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={"client_id": client_id, "client_secret": secret, "scope": "https://management.azure.com/.default", "grant_type": "client_credentials"},
                timeout=20,
            )
            token.raise_for_status()
            access = token.json()["access_token"]
            sub = requests.get(
                f"https://management.azure.com/subscriptions/{subscription}?api-version=2022-12-01",
                headers={"Authorization": f"Bearer {access}"},
                timeout=20,
            )
            sub.raise_for_status()
            body = sub.json()
            return {"ok": True, "account": body.get("subscriptionId"), "name": body.get("displayName")}
        except (requests.RequestException, KeyError, ValueError) as error:
            return {"ok": False, "detail": str(error)[:240]}
    if provider == "gcp":
        if not creds.get("project_id"):
            return {"ok": False, "detail": "GCP account needs project_id. Live token verify needs google-auth on the host."}
        try:
            import google.auth
            from google.auth.transport.requests import Request as GoogleRequest
        except ImportError:
            return {"ok": False, "detail": "google-auth is not installed. Project stored, token not verified."}
        try:
            credentials, project = google.auth.default()
            credentials.refresh(GoogleRequest())
            return {"ok": True, "account": creds.get("project_id") or project}
        except Exception as error:
            return {"ok": False, "detail": str(error)[:240]}
    return {"ok": False, "detail": "Unsupported provider."}


def _sync_azure_prices() -> dict:
    changed = processed = 0
    queries = [
        "armRegionName eq 'centralindia' and serviceName eq 'Azure Container Apps' and priceType eq 'Consumption'",
        "armRegionName eq 'centralindia' and contains(serviceName, 'PostgreSQL') and priceType eq 'Consumption'",
    ]
    for filt in queries:
        url = "https://prices.azure.com/api/retail/prices?$filter=" + quote(filt)
        try:
            response = requests.get(url, timeout=25)
            response.raise_for_status()
            items = (response.json() or {}).get("Items") or []
        except (requests.RequestException, ValueError):
            continue
        for item in items[:40]:
            sku = str(item.get("armSkuName") or item.get("skuName") or item.get("meterName") or "")[:120]
            if not sku:
                continue
            processed += 1
            _upsert_simple(
                "cloud_prices",
                {
                    "provider": "azure",
                    "service": "container_apps" if "Container" in str(item.get("serviceName")) else "azure_database_postgresql",
                    "sku": sku,
                    "region": str(item.get("armRegionName") or "centralindia"),
                    "unit": str(item.get("unitOfMeasure") or "hour")[:40],
                    "price_type": str(item.get("priceType") or "Consumption")[:40],
                },
                {
                    "currency": str(item.get("currencyCode") or "USD"),
                    "unit_quantity": 1,
                    "price": float(item.get("retailPrice") or 0),
                    "pricing_model": "provider",
                    "source": "azure_retail",
                    "raw_json": json_text({"meterName": item.get("meterName"), "productName": item.get("productName")}),
                    "last_synced_at": now_iso(),
                },
            )
            changed += 1
    return {"processed": processed, "changed": changed}


def _sync_aws_services() -> dict:
    try:
        response = requests.get("https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/index.json", timeout=30)
        response.raise_for_status()
        offers = (response.json() or {}).get("offers") or {}
    except (requests.RequestException, ValueError) as error:
        return {"processed": 0, "changed": 0, "error": str(error)[:200]}
    changed = 0
    for code, meta in list(offers.items())[:80]:
        _upsert_simple(
            "cloud_services",
            {"provider": "aws", "service_code": str(code)[:80]},
            {"service_name": str((meta or {}).get("offerCode") or code)[:160], "last_synced_at": now_iso(), "status": "catalog"},
        )
        changed += 1
    return {"processed": changed, "changed": changed}


def _sync_gcp_services() -> dict:
    key = env("GOOGLE_API_KEY") or env("GCP_BILLING_API_KEY")
    if not key:
        return {"processed": 0, "changed": 0, "skipped": "no GOOGLE_API_KEY"}
    try:
        response = requests.get("https://cloudbilling.googleapis.com/v1/services", params={"key": key, "pageSize": 50}, timeout=25)
        response.raise_for_status()
        services = (response.json() or {}).get("services") or []
    except (requests.RequestException, ValueError) as error:
        return {"processed": 0, "changed": 0, "error": str(error)[:200]}
    changed = 0
    for service in services:
        code = str(service.get("serviceId") or service.get("name") or "")[:80]
        if not code:
            continue
        _upsert_simple(
            "cloud_services",
            {"provider": "gcp", "service_code": code},
            {"service_name": str(service.get("displayName") or code)[:160], "last_synced_at": now_iso(), "status": "catalog"},
        )
        changed += 1
    return {"processed": changed, "changed": changed}


def _poll_operations() -> dict:
    rows = query("SELECT * FROM cloud_operations WHERE status = 'pending' ORDER BY started_at ASC LIMIT 20")
    closed = 0
    for row in rows:
        if row.get("operation_type") == "apply" and not env_flag(APPLY_FLAG):
            execute(
                "UPDATE cloud_operations SET status = 'blocked', error_message = ?, completed_at = ? WHERE id = ?",
                ("CLOUD_ALLOW_APPLY is off.", now_iso(), row["id"]),
            )
            closed += 1
    return {"processed": len(rows), "changed": closed}


def cloud_worker() -> dict:
    ensure_cloud_tables()
    due = query(
        "SELECT * FROM cloud_sync_jobs WHERE next_run_at <= ? AND status <> 'running' ORDER BY next_run_at ASC LIMIT 3",
        (now_iso(),),
    )
    results = []
    for job in due:
        execute("UPDATE cloud_sync_jobs SET status = 'running', last_run_at = ? WHERE id = ?", (now_iso(), job["id"]))
        try:
            if job["job_type"] == "azure_prices":
                outcome = _sync_azure_prices()
            elif job["job_type"] == "aws_services":
                outcome = _sync_aws_services()
            elif job["job_type"] == "gcp_services":
                outcome = _sync_gcp_services()
            elif job["job_type"] == "operations":
                outcome = _poll_operations()
            else:
                outcome = {"processed": 0, "changed": 0, "skipped": "health uses stored resources only"}
            execute(
                "UPDATE cloud_sync_jobs SET status = 'idle', next_run_at = ?, records_processed = ?, records_changed = ?, error_count = 0, error_message = '' WHERE id = ?",
                (hours_from_now(JOB_HOURS.get(job["job_type"], 1)), int(outcome.get("processed") or 0), int(outcome.get("changed") or 0), job["id"]),
            )
            results.append({"job": job["job_type"], **outcome})
        except Exception as error:
            execute(
                "UPDATE cloud_sync_jobs SET status = 'idle', next_run_at = ?, error_count = error_count + 1, error_message = ? WHERE id = ?",
                (hours_from_now(0.25), str(error)[:400], job["id"]),
            )
            results.append({"job": job["job_type"], "error": str(error)[:200]})
    return {"due": len(due), "results": results}


cloud_router = APIRouter(prefix="/api/cloud", tags=["cloud"])


@cloud_router.get("/providers")
def list_providers(user: dict = Depends(user_from_header)) -> list:
    return query("SELECT provider, name, status, last_sync_at FROM cloud_providers ORDER BY provider")


@cloud_router.get("/services")
def list_services(provider: str = "", user: dict = Depends(user_from_header)) -> list:
    if provider:
        return query("SELECT provider, service_code, service_name, status FROM cloud_services WHERE provider = ? ORDER BY service_code LIMIT 200", (provider,))
    return query("SELECT provider, service_code, service_name, status FROM cloud_services ORDER BY provider, service_code LIMIT 200")


@cloud_router.get("/regions")
def list_regions(provider: str = "", user: dict = Depends(user_from_header)) -> list:
    if provider:
        return query("SELECT provider, region_code, region_name, country FROM cloud_regions WHERE provider = ? ORDER BY region_code", (provider,))
    return query("SELECT provider, region_code, region_name, country FROM cloud_regions ORDER BY provider, region_code")


@cloud_router.get("/capabilities")
def list_capabilities(user: dict = Depends(user_from_header)) -> list:
    return query("SELECT source, target, compatibility, score, notes FROM cloud_compatibility ORDER BY score DESC")


@cloud_router.get("/pricing")
def list_pricing(provider: str = "", user: dict = Depends(user_from_header)) -> list:
    if provider:
        return query(
            "SELECT provider, service, sku, region, currency, unit, price, price_type, source FROM cloud_prices WHERE provider = ? ORDER BY service, sku LIMIT 200",
            (provider,),
        )
    return query("SELECT provider, service, sku, region, currency, unit, price, price_type, source FROM cloud_prices ORDER BY provider, service LIMIT 200")


@cloud_router.get("/accounts")
def list_accounts(user: dict = Depends(user_from_header)) -> list:
    rows = query(
        "SELECT id, provider, account_name, account_identifier, credential_type, status, last_verified_at, created_at FROM cloud_accounts WHERE workspace_id = ? ORDER BY created_at DESC",
        (user["workspace_id"],),
    )
    return rows


@cloud_router.post("/accounts")
def save_account(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    provider = str(payload.get("provider") or "").lower()
    if provider not in CLOUD_PROVIDERS:
        raise HTTPException(400, "Provider must be aws, gcp, or azure.")
    identifier = str(payload.get("account_identifier") or "").strip()
    if not identifier:
        raise HTTPException(400, "Account identifier is required.")
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    account_id = new_id()
    execute(
        "INSERT INTO cloud_accounts (id, workspace_id, provider, account_name, account_identifier, credential_type, credentials_encrypted, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (
            account_id,
            user["workspace_id"],
            provider,
            str(payload.get("account_name") or identifier)[:160],
            identifier[:160],
            str(payload.get("credential_type") or "service_principal")[:40],
            encrypt_secret(json_text(credentials)) if credentials else "",
            now_iso(),
            now_iso(),
        ),
    )
    return {"id": account_id, "ok": True}


@cloud_router.post("/accounts/{account_id}/verify")
def verify_account(account_id: str, user: dict = Depends(user_from_header)) -> dict:
    row = _account_or_404(user["workspace_id"], account_id)
    result = _verify_account(row)
    status = "verified" if result.get("ok") else "pending"
    execute(
        "UPDATE cloud_accounts SET status = ?, last_verified_at = ?, account_identifier = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
        (status, now_iso(), result.get("account") or row.get("account_identifier"), now_iso(), account_id, user["workspace_id"]),
    )
    return {"ok": bool(result.get("ok")), "status": status, "detail": result.get("detail") or result.get("name") or ""}


@cloud_router.delete("/accounts/{account_id}")
def delete_account(account_id: str, user: dict = Depends(user_from_header)) -> dict:
    _account_or_404(user["workspace_id"], account_id)
    execute("DELETE FROM cloud_accounts WHERE id = ? AND workspace_id = ?", (account_id, user["workspace_id"]))
    return {"ok": True}


@cloud_router.post("/analyze")
def analyze(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    text = str(payload.get("message") or "")
    req = _extract_requirements(text, None, payload.get("requirements") if isinstance(payload.get("requirements"), dict) else None)
    return _recommend(user["workspace_id"], req, None)


@cloud_router.post("/answer")
def answer(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    plan_id = str(payload.get("plan_id") or "")
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    req = _apply_answer(_load(row.get("requirements_json"), {}), str(payload.get("field") or ""), str(payload.get("value") or ""))
    if payload.get("message"):
        req = _extract_requirements(str(payload.get("message")), req, None)
    return _recommend(user["workspace_id"], req, plan_id)


@cloud_router.post("/recommend")
def recommend(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    plan_id = str(payload.get("plan_id") or "")
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    return _recommend(user["workspace_id"], _load(row.get("requirements_json"), {}), plan_id)


@cloud_router.get("/plans")
def list_plans(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT id, provider, architecture_id, status, approval_status, confidence, updated_at FROM cloud_plans WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 50",
        (user["workspace_id"],),
    )


@cloud_router.get("/plans/{plan_id}")
def get_plan(plan_id: str, user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    return _public_plan(row)


@cloud_router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    step = str(payload.get("step") or "architecture")
    if step not in {"architecture", "plan", "deploy"}:
        raise HTTPException(400, "Approval step must be architecture, plan, or deploy.")
    status = {"architecture": "plan_generated", "plan": "awaiting_deployment_approval", "deploy": "awaiting_deployment_approval"}[step]
    execute(
        "UPDATE cloud_plans SET approval_status = ?, status = ?, updated_at = ? WHERE id = ? AND workspace_id = ?",
        (step + "_approved", status, now_iso(), plan_id, user["workspace_id"]),
    )
    return {"ok": True, "approval_status": step + "_approved"}


@cloud_router.post("/plans/{plan_id}/reject")
def reject_plan(plan_id: str, user: dict = Depends(user_from_header)) -> dict:
    execute(
        "UPDATE cloud_plans SET status = 'draft', approval_status = 'rejected', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), plan_id, user["workspace_id"]),
    )
    return {"ok": True}


@cloud_router.post("/plans/{plan_id}/modify")
def modify_plan(plan_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    req = _extract_requirements(str(payload.get("message") or ""), _load(row.get("requirements_json"), {}), payload.get("requirements") if isinstance(payload.get("requirements"), dict) else None)
    return _recommend(user["workspace_id"], req, plan_id)


@cloud_router.post("/plans/{plan_id}/terraform")
def plan_terraform(plan_id: str, user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    terraform = row.get("terraform_json") or ""
    binary = shutil.which("terraform")
    if not binary:
        return {"terraform": terraform, "validated": False, "detail": "terraform CLI is not installed. Template returned, not applied."}
    with tempfile.TemporaryDirectory() as folder:
        Path(folder, "main.tf").write_text(terraform, encoding="utf-8")
        try:
            proc = subprocess.run([binary, "fmt", "-check", "main.tf"], cwd=folder, capture_output=True, text=True, timeout=20)
        except subprocess.TimeoutExpired:
            return {"terraform": terraform, "validated": False, "detail": "terraform fmt timed out."}
    return {"terraform": terraform, "validated": proc.returncode == 0, "detail": (proc.stderr or proc.stdout or "")[:400]}


@cloud_router.post("/plans/{plan_id}/deploy")
def deploy_plan(plan_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    row = one("SELECT * FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"]))
    if not row:
        raise HTTPException(404, "Plan not found.")
    if row.get("approval_status") != "deploy_approved":
        raise HTTPException(400, "Approve the deploy step before execution.")
    security = _load(row.get("security_json"), {})
    if any(item.get("severity") == "high" for item in security.get("findings") or []) and not payload.get("accept_risk"):
        raise HTTPException(400, "High security findings require accept_risk=true.")
    account_id = str(payload.get("account_id") or "")
    account = _account_or_404(user["workspace_id"], account_id) if account_id else None
    if not account or account.get("status") != "verified":
        raise HTTPException(400, "A verified cloud account is required.")
    if account.get("provider") != row.get("provider"):
        raise HTTPException(400, "Account provider does not match the plan.")
    op_id = new_id()
    if not env_flag(APPLY_FLAG):
        execute(
            "INSERT INTO cloud_operations (id, workspace_id, plan_id, provider, operation_type, request_json, status, error_message, started_at, completed_at) "
            "VALUES (?, ?, ?, ?, 'apply', ?, 'blocked', ?, ?, ?)",
            (op_id, user["workspace_id"], plan_id, row.get("provider") or "", json_text({"account_id": account_id}), "CLOUD_ALLOW_APPLY is off. No provider write was sent.", now_iso(), now_iso()),
        )
        return {"ok": False, "operation_id": op_id, "status": "blocked", "detail": "Apply is disabled. No provider write was sent."}
    execute(
        "INSERT INTO cloud_operations (id, workspace_id, plan_id, provider, operation_type, request_json, status, started_at) VALUES (?, ?, ?, ?, 'apply', ?, 'pending', ?)",
        (op_id, user["workspace_id"], plan_id, row.get("provider") or "", json_text({"account_id": account_id, "note": "adapter executes only reviewed templates"}), now_iso()),
    )
    execute(
        "UPDATE cloud_plans SET status = 'applying', updated_at = ? WHERE id = ? AND workspace_id = ?",
        (now_iso(), plan_id, user["workspace_id"]),
    )
    return {"ok": True, "operation_id": op_id, "status": "pending", "detail": "Operation recorded. Provider adapter runs on the worker only for reviewed templates."}


@cloud_router.get("/resources")
def list_resources(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT id, provider, resource_type, resource_name, region, status, health_status, last_seen_at FROM cloud_resources WHERE workspace_id = ? ORDER BY last_seen_at DESC LIMIT 200",
        (user["workspace_id"],),
    )


@cloud_router.get("/cost")
def cost_summary(user: dict = Depends(user_from_header)) -> dict:
    plans = query(
        "SELECT id, provider, architecture_id, cost_json, status FROM cloud_plans WHERE workspace_id = ? ORDER BY updated_at DESC LIMIT 20",
        (user["workspace_id"],),
    )
    for plan in plans:
        plan["cost"] = _load(plan.pop("cost_json"), {})
    return {"plans": plans, "note": "Estimates use cloud_prices. source=seed is not an invoice. source=azure_retail is catalog retail."}


@cloud_router.post("/feedback")
def save_feedback(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    plan_id = str(payload.get("plan_id") or "")
    if plan_id and not one("SELECT id FROM cloud_plans WHERE id = ? AND workspace_id = ?", (plan_id, user["workspace_id"])):
        raise HTTPException(404, "Plan not found.")
    feedback_id = new_id()
    execute(
        "INSERT INTO cloud_feedback (id, workspace_id, plan_id, feedback_type, expected, actual, rating, reason, user_comment, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            feedback_id,
            user["workspace_id"],
            plan_id,
            str(payload.get("feedback_type") or "recommendation")[:40],
            str(payload.get("expected") or "")[:160],
            str(payload.get("actual") or "")[:160],
            int(payload.get("rating") or 0) or None,
            str(payload.get("reason") or "")[:240],
            str(payload.get("user_comment") or "")[:1000],
            now_iso(),
        ),
    )
    return {"id": feedback_id, "ok": True}


@cloud_router.get("/feedback")
def list_feedback(user: dict = Depends(user_from_header)) -> list:
    return query(
        "SELECT id, plan_id, feedback_type, rating, reason, created_at FROM cloud_feedback WHERE workspace_id = ? ORDER BY created_at DESC LIMIT 50",
        (user["workspace_id"],),
    )