from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse, urldefrag
from urllib.robotparser import RobotFileParser

import requests
from fastapi import APIRouter, Depends, HTTPException

from auth import user_from_header
from db import env, env_flag, execute, new_id, now_iso, one, query, setting

router = APIRouter(prefix="/api/search-intelligence", tags=["search-intelligence"])

PROMPT_VERSION = 1
MAX_PAGES = 30
MAX_BYTES = 1_200_000
FETCH_TIMEOUT = 12
MIN_PATTERN_SAMPLE = 8
ACTIVATE_SAMPLE = 20
ACTIVATE_SUCCESS = 0.7
USER_AGENT = "Revenue360sSearchBot/1.0"

SYSTEM_PROMPT = """You are the Revenue360s Search Intelligence Decision Engine.
Analyze only the evidence supplied. You are not a crawler and you are not a source of measurements.
Never invent rankings, traffic, backlinks, citations, search volume, pricing, file paths, or line numbers.
Separate observed facts from inference. Use null when evidence is missing.
Do not promise rankings, AI mentions, traffic, or revenue.
Prefer few high-impact actions. Classify each action as SAFE_AUTOMATION, USER_APPROVAL, MANUAL_ACTION, or INSUFFICIENT_DATA.
SAFE_AUTOMATION is only for deterministic low-risk plans: metadata, structured data, robots.txt, sitemap, unambiguous internal links.
Return only valid JSON matching the requested schema. No markdown."""

TASK_PLAN = """TASK: From the supplied crawl findings and page evidence, extract the business, entities, topics, queries, and competitor hypotheses, then attach root causes and actions to existing finding codes only.
Schema:
{"business":{"company_name":null,"industry":null,"business_type":null,"description":null,"products":[],"services":[],"audience":[],"locations":[],"countries":[],"confidence":0,"evidence":null},
"entities":[{"entity_type":"organization","entity_name":"","description":null,"confidence":0}],
"topics":[{"topic":"","intent":"informational","importance":0,"relevance_score":0,"confidence":0}],
"queries":[{"query":"","query_type":"informational","intent":"informational","commercial_value":0,"relevance_score":0}],
"competitors":[{"domain":"","name":null,"type":"serp","confidence":0,"reason":null}],
"opportunities":[{"finding_codes":[],"title":"","description":"","root_cause":"relevance","impact_score":0,"confidence_score":0,"effort_score":0}],
"actions":[{"opportunity_title":"","action_type":"metadata_title","title":"","description":"","expected_impact":"","effort":0,"classification":"USER_APPROVAL"}]}
root_cause must be one of: technical_accessibility, indexing, relevance, search_intent, topical_coverage, internal_authority, external_authority, entity_clarity, ai_citation, ai_answer_eligibility, competitive_strength, content_quality, freshness, conversion.
Scores are 0-100 except confidence which is 0-1."""

ISSUE_ACTIONS = {
    "missing_title": ("metadata_title", "Add a unique title", "SAFE_AUTOMATION", 2),
    "short_title": ("metadata_title", "Expand the title to describe the page", "SAFE_AUTOMATION", 2),
    "duplicate_title": ("metadata_title", "Make duplicate titles unique", "USER_APPROVAL", 3),
    "missing_meta_description": ("metadata_description", "Add a meta description", "SAFE_AUTOMATION", 2),
    "missing_h1": ("heading", "Add one descriptive H1", "USER_APPROVAL", 2),
    "thin_content": ("expand_content", "Expand thin page with first-party detail", "USER_APPROVAL", 4),
    "missing_canonical": ("canonical", "Add a self-referencing canonical", "SAFE_AUTOMATION", 2),
    "canonical_mismatch": ("canonical", "Align canonical with the indexable URL", "USER_APPROVAL", 3),
    "noindex": ("indexability", "Review noindex on a URL that should be searchable", "USER_APPROVAL", 3),
    "http_error": ("http_status", "Fix the non-200 URL or stop linking to it", "MANUAL_ACTION", 3),
    "missing_schema": ("schema_organization", "Add Organization or WebPage structured data", "SAFE_AUTOMATION", 2),
    "slow_response": ("performance", "Reduce server response time", "MANUAL_ACTION", 4),
    "missing_sitemap": ("sitemap", "Publish a sitemap and reference it from robots.txt", "SAFE_AUTOMATION", 2),
    "robots_block": ("robots_txt", "Stop robots.txt from blocking indexable URLs", "USER_APPROVAL", 3),
    "missing_entity_clarity": ("entity_page", "State the organization, offer, and location in first-party copy", "USER_APPROVAL", 3),
}

DEFAULT_WEIGHTS = {
    "missing_title": 70, "short_title": 40, "duplicate_title": 55, "missing_meta_description": 45,
    "missing_h1": 40, "thin_content": 55, "missing_canonical": 50, "canonical_mismatch": 60,
    "noindex": 90, "http_error": 85, "missing_schema": 35, "slow_response": 40,
    "missing_sitemap": 30, "robots_block": 95, "missing_entity_clarity": 50,
}


def ensure_tables() -> None:
    statements = [
        """CREATE TABLE IF NOT EXISTS search_projects (
            id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL,
            website_url VARCHAR(500) NOT NULL, domain VARCHAR(255) NOT NULL,
            business_name VARCHAR(255) NOT NULL DEFAULT '', industry VARCHAR(120) NOT NULL DEFAULT '',
            description TEXT, target_country VARCHAR(80) NOT NULL DEFAULT '',
            target_locations LONGTEXT, competitors_seed LONGTEXT, keywords_seed LONGTEXT,
            target_audience TEXT, settings LONGTEXT, status VARCHAR(40) NOT NULL DEFAULT 'active',
            last_analyzed_at VARCHAR(40) NOT NULL DEFAULT '', created_at VARCHAR(40) NOT NULL,
            updated_at VARCHAR(40) NOT NULL DEFAULT '', INDEX idx_search_proj (workspace_id, domain))""",
        """CREATE TABLE IF NOT EXISTS search_analysis_runs (
            id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL, project_id VARCHAR(36) NOT NULL,
            status VARCHAR(40) NOT NULL DEFAULT 'queued', phase VARCHAR(40) NOT NULL DEFAULT 'queued',
            progress LONGTEXT, error TEXT, llm_status VARCHAR(40) NOT NULL DEFAULT '',
            created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL DEFAULT '',
            completed_at VARCHAR(40) NOT NULL DEFAULT '', INDEX idx_search_run (project_id, created_at))""",
        """CREATE TABLE IF NOT EXISTS website_crawls (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            started_at VARCHAR(40) NOT NULL, completed_at VARCHAR(40) NOT NULL DEFAULT '',
            status VARCHAR(40) NOT NULL DEFAULT 'running', pages_discovered INT NOT NULL DEFAULT 0,
            pages_crawled INT NOT NULL DEFAULT 0, crawl_version INT NOT NULL DEFAULT 1,
            error_count INT NOT NULL DEFAULT 0, robots_status VARCHAR(40) NOT NULL DEFAULT '',
            sitemap_status VARCHAR(40) NOT NULL DEFAULT '')""",
        """CREATE TABLE IF NOT EXISTS website_pages (
            id VARCHAR(36) PRIMARY KEY, crawl_id VARCHAR(36) NOT NULL, url VARCHAR(1000) NOT NULL,
            status_code INT NOT NULL DEFAULT 0, content_hash VARCHAR(64) NOT NULL DEFAULT '',
            title VARCHAR(500) NOT NULL DEFAULT '', meta_description VARCHAR(500) NOT NULL DEFAULT '',
            canonical_url VARCHAR(1000) NOT NULL DEFAULT '', h1 VARCHAR(500) NOT NULL DEFAULT '',
            word_count INT NOT NULL DEFAULT 0, language VARCHAR(20) NOT NULL DEFAULT '',
            indexable TINYINT NOT NULL DEFAULT 0, robots_allowed TINYINT NOT NULL DEFAULT 1,
            load_time DOUBLE NOT NULL DEFAULT 0, schema_types VARCHAR(500) NOT NULL DEFAULT '',
            content MEDIUMTEXT, INDEX idx_pages_crawl (crawl_id))""",
        """CREATE TABLE IF NOT EXISTS business_profiles (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            company_name VARCHAR(255) NOT NULL DEFAULT '', industry VARCHAR(120) NOT NULL DEFAULT '',
            business_type VARCHAR(120) NOT NULL DEFAULT '', description TEXT, products LONGTEXT,
            services LONGTEXT, audience LONGTEXT, locations LONGTEXT, countries LONGTEXT,
            confidence_score DOUBLE NOT NULL DEFAULT 0, source VARCHAR(40) NOT NULL DEFAULT 'deterministic',
            evidence TEXT, created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS search_entities (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            entity_type VARCHAR(40) NOT NULL, entity_name VARCHAR(255) NOT NULL,
            description TEXT, source VARCHAR(40) NOT NULL DEFAULT 'deterministic', confidence DOUBLE NOT NULL DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS search_topics (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            topic VARCHAR(255) NOT NULL, parent_topic_id VARCHAR(36) NOT NULL DEFAULT '',
            intent VARCHAR(40) NOT NULL DEFAULT '', importance DOUBLE NOT NULL DEFAULT 0,
            relevance_score DOUBLE NOT NULL DEFAULT 0, confidence DOUBLE NOT NULL DEFAULT 0, source VARCHAR(40) NOT NULL DEFAULT 'deterministic')""",
        """CREATE TABLE IF NOT EXISTS search_queries (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            query VARCHAR(500) NOT NULL, query_type VARCHAR(40) NOT NULL DEFAULT 'informational',
            intent VARCHAR(40) NOT NULL DEFAULT '', topic_id VARCHAR(36) NOT NULL DEFAULT '',
            commercial_value DOUBLE NOT NULL DEFAULT 0, relevance_score DOUBLE NOT NULL DEFAULT 0,
            discovered_from VARCHAR(40) NOT NULL DEFAULT 'user', status VARCHAR(40) NOT NULL DEFAULT 'active')""",
        """CREATE TABLE IF NOT EXISTS competitors (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            domain VARCHAR(255) NOT NULL, name VARCHAR(255) NOT NULL DEFAULT '',
            type VARCHAR(40) NOT NULL DEFAULT 'direct', confidence DOUBLE NOT NULL DEFAULT 0,
            discovery_source VARCHAR(40) NOT NULL DEFAULT 'user', status VARCHAR(40) NOT NULL DEFAULT 'active',
            reason TEXT)""",
        """CREATE TABLE IF NOT EXISTS competitor_observations (
            id VARCHAR(36) PRIMARY KEY, competitor_id VARCHAR(36) NOT NULL, query_id VARCHAR(36) NOT NULL DEFAULT '',
            position DOUBLE, visibility DOUBLE, content_score DOUBLE, authority_score DOUBLE,
            ai_visibility DOUBLE, note VARCHAR(500) NOT NULL DEFAULT '', observed_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS seo_findings (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, crawl_id VARCHAR(36) NOT NULL,
            page_id VARCHAR(36) NOT NULL DEFAULT '', category VARCHAR(40) NOT NULL, severity VARCHAR(20) NOT NULL,
            issue_code VARCHAR(60) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT, evidence TEXT,
            impact_score DOUBLE NOT NULL DEFAULT 0, confidence_score DOUBLE NOT NULL DEFAULT 1,
            status VARCHAR(40) NOT NULL DEFAULT 'open', created_at VARCHAR(40) NOT NULL,
            INDEX idx_findings_proj (project_id, issue_code))""",
        """CREATE TABLE IF NOT EXISTS aeo_questions (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            query VARCHAR(500) NOT NULL, intent VARCHAR(40) NOT NULL DEFAULT 'ai_question',
            topic_id VARCHAR(36) NOT NULL DEFAULT '', priority DOUBLE NOT NULL DEFAULT 0,
            source VARCHAR(40) NOT NULL DEFAULT 'deterministic', status VARCHAR(40) NOT NULL DEFAULT 'unmeasured')""",
        """CREATE TABLE IF NOT EXISTS aeo_observations (
            id VARCHAR(36) PRIMARY KEY, question_id VARCHAR(36) NOT NULL, provider VARCHAR(40) NOT NULL,
            run_id VARCHAR(36) NOT NULL, brand_mentioned TINYINT, brand_position INT,
            competitors_mentioned LONGTEXT, citation_found TINYINT, citation_sources LONGTEXT,
            sentiment VARCHAR(40) NOT NULL DEFAULT '', confidence DOUBLE NOT NULL DEFAULT 0, observed_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS geo_findings (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            code VARCHAR(60) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT, evidence TEXT,
            score DOUBLE NOT NULL DEFAULT 0, confidence DOUBLE NOT NULL DEFAULT 1, status VARCHAR(40) NOT NULL DEFAULT 'open')""",
        """CREATE TABLE IF NOT EXISTS growth_opportunities (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, run_id VARCHAR(36) NOT NULL,
            type VARCHAR(40) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT, root_cause VARCHAR(60) NOT NULL DEFAULT '',
            evidence TEXT, impact_score DOUBLE NOT NULL DEFAULT 0, confidence_score DOUBLE NOT NULL DEFAULT 0,
            effort_score DOUBLE NOT NULL DEFAULT 0, priority_score DOUBLE NOT NULL DEFAULT 0,
            affected_pages LONGTEXT, affected_queries LONGTEXT, affected_competitors LONGTEXT,
            finding_ids LONGTEXT, source VARCHAR(40) NOT NULL DEFAULT 'rules', status VARCHAR(40) NOT NULL DEFAULT 'open',
            created_at VARCHAR(40) NOT NULL, INDEX idx_opp (project_id, priority_score))""",
        """CREATE TABLE IF NOT EXISTS growth_actions (
            id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, project_id VARCHAR(36) NOT NULL,
            action_type VARCHAR(60) NOT NULL, title VARCHAR(255) NOT NULL, description TEXT,
            expected_impact VARCHAR(255) NOT NULL DEFAULT '', effort DOUBLE NOT NULL DEFAULT 0,
            automatable TINYINT NOT NULL DEFAULT 0, requires_user TINYINT NOT NULL DEFAULT 1,
            classification VARCHAR(40) NOT NULL DEFAULT 'USER_APPROVAL', status VARCHAR(40) NOT NULL DEFAULT 'proposed',
            created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS execution_jobs (
            id VARCHAR(36) PRIMARY KEY, action_id VARCHAR(36) NOT NULL, project_id VARCHAR(36) NOT NULL,
            execution_type VARCHAR(60) NOT NULL, provider VARCHAR(40) NOT NULL DEFAULT '',
            status VARCHAR(40) NOT NULL DEFAULT 'queued', started_at VARCHAR(40) NOT NULL DEFAULT '',
            completed_at VARCHAR(40) NOT NULL DEFAULT '', error TEXT, result LONGTEXT, created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS action_outcomes (
            id VARCHAR(36) PRIMARY KEY, action_id VARCHAR(36) NOT NULL, project_id VARCHAR(36) NOT NULL,
            metric VARCHAR(60) NOT NULL, baseline_value DOUBLE, target_value DOUBLE, actual_value DOUBLE,
            change_pct DOUBLE, measurement_window VARCHAR(40) NOT NULL DEFAULT '', outcome VARCHAR(40) NOT NULL DEFAULT '',
            confidence DOUBLE NOT NULL DEFAULT 0, measured_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS feedback_events (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, action_id VARCHAR(36) NOT NULL DEFAULT '',
            feedback_type VARCHAR(40) NOT NULL, rating INT, feedback_text TEXT,
            source VARCHAR(40) NOT NULL DEFAULT 'user', created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS learning_observations (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL, action_type VARCHAR(60) NOT NULL,
            industry VARCHAR(120) NOT NULL DEFAULT '', business_type VARCHAR(120) NOT NULL DEFAULT '',
            condition_key VARCHAR(120) NOT NULL, action VARCHAR(255) NOT NULL, outcome VARCHAR(40) NOT NULL,
            success_score DOUBLE NOT NULL DEFAULT 0, created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS learning_patterns (
            id VARCHAR(36) PRIMARY KEY, pattern_type VARCHAR(40) NOT NULL, segment VARCHAR(120) NOT NULL DEFAULT '',
            condition_key VARCHAR(120) NOT NULL, recommended_action VARCHAR(255) NOT NULL,
            success_rate DOUBLE NOT NULL DEFAULT 0, avg_impact DOUBLE NOT NULL DEFAULT 0, avg_effort DOUBLE NOT NULL DEFAULT 0,
            sample_size INT NOT NULL DEFAULT 0, confidence DOUBLE NOT NULL DEFAULT 0, status VARCHAR(40) NOT NULL DEFAULT 'watching',
            updated_at VARCHAR(40) NOT NULL, UNIQUE KEY uq_pattern (pattern_type, segment, condition_key, recommended_action))""",
        """CREATE TABLE IF NOT EXISTS strategy_versions (
            id VARCHAR(36) PRIMARY KEY, strategy_name VARCHAR(80) NOT NULL, version INT NOT NULL,
            parameters LONGTEXT, reason TEXT, active TINYINT NOT NULL DEFAULT 0, status VARCHAR(40) NOT NULL DEFAULT 'candidate',
            created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS llm_runs (
            id VARCHAR(36) PRIMARY KEY, project_id VARCHAR(36) NOT NULL DEFAULT '', task_type VARCHAR(60) NOT NULL,
            provider VARCHAR(40) NOT NULL DEFAULT '', model VARCHAR(80) NOT NULL DEFAULT '', prompt_version INT NOT NULL DEFAULT 1,
            input_hash VARCHAR(64) NOT NULL DEFAULT '', output LONGTEXT, status VARCHAR(40) NOT NULL,
            latency_ms INT NOT NULL DEFAULT 0, input_tokens INT NOT NULL DEFAULT 0, output_tokens INT NOT NULL DEFAULT 0,
            estimated_cost DOUBLE, error_code VARCHAR(80) NOT NULL DEFAULT '', created_at VARCHAR(40) NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS llm_prompts (
            id VARCHAR(36) PRIMARY KEY, name VARCHAR(80) NOT NULL, version INT NOT NULL,
            system_prompt MEDIUMTEXT, task_prompt MEDIUMTEXT, schema_version INT NOT NULL DEFAULT 1,
            active TINYINT NOT NULL DEFAULT 1, created_at VARCHAR(40) NOT NULL,
            UNIQUE KEY uq_prompt (name, version))""",
    ]
    for statement in statements:
        execute(statement)
    if not one("SELECT id FROM llm_prompts WHERE name = ? AND version = ?", ("search_decision", PROMPT_VERSION)):
        execute(
            "INSERT INTO llm_prompts (id, name, version, system_prompt, task_prompt, schema_version, active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
            (new_id(), "search_decision", PROMPT_VERSION, SYSTEM_PROMPT, TASK_PLAN, 1, now_iso()),
        )
    if not one("SELECT id FROM strategy_versions WHERE strategy_name = ? AND active = 1", ("issue_weights",)):
        execute(
            "INSERT INTO strategy_versions (id, strategy_name, version, parameters, reason, active, status, created_at) VALUES (?, ?, 1, ?, ?, 1, 'active', ?)",
            (new_id(), "issue_weights", json.dumps(DEFAULT_WEIGHTS), "Initial deterministic weights.", now_iso()),
        )


def _json(value, default):
    try:
        loaded = json.loads(value) if isinstance(value, str) else value
        return loaded if loaded is not None else default
    except (TypeError, json.JSONDecodeError):
        return default


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _clamp(value, low, high, default=0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, number))


def _project(workspace_id: str, project_id: str) -> dict:
    row = one("SELECT * FROM search_projects WHERE id = ? AND workspace_id = ?", (project_id, workspace_id))
    if not row:
        raise HTTPException(404, "Search project not found.")
    return row


def _weights() -> dict:
    row = one("SELECT parameters FROM strategy_versions WHERE strategy_name = 'issue_weights' AND active = 1 ORDER BY version DESC LIMIT 1")
    weights = _json((row or {}).get("parameters"), {})
    return {**DEFAULT_WEIGHTS, **weights} if isinstance(weights, dict) else dict(DEFAULT_WEIGHTS)


def _default_base(provider: str) -> str:
    return {
        "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "openai": "https://api.openai.com/v1",
        "deepseek": "https://api.deepseek.com",
        "openrouter": "https://openrouter.ai/api/v1",
    }.get(provider, "https://dashscope.aliyuncs.com/compatible-mode/v1")


def llm_settings(workspace_id: str) -> dict:
    del workspace_id
    explicit = env("LLM_ENABLED", "").strip().lower()
    provider = (env("LLM_PROVIDER", "qwen") or "qwen").strip().lower()
    model = env("LLM_MODEL", "qwen-plus")
    api_key = env("LLM_API_KEY", "")
    base = env("LLM_BASE_URL", _default_base(provider)).rstrip("/")
    if explicit in {"0", "false", "no", "off"}:
        enabled = False
    elif explicit in {"1", "true", "yes", "on"}:
        enabled = True
    else:
        enabled = bool(api_key)
    return {"enabled": enabled and bool(api_key), "provider": provider, "model": model, "api_key": api_key, "base_url": base,
            "reason": "" if api_key and explicit not in {"0", "false", "no", "off"} else "llm_not_configured"}


def run_llm(task: str, system_prompt: str, input_data: dict, response_schema: dict | None = None, workspace_id: str = "", project_id: str = "") -> dict:
    """Single provider boundary. Returns a structured result and never raises for a missing provider."""
    config = llm_settings(workspace_id)
    payload = {"task": task, "input": input_data, "schema": response_schema}
    digest = hashlib.sha256(_dump(payload).encode()).hexdigest()
    if not config["enabled"]:
        result = {"available": False, "reason": config["reason"] or "llm_not_configured", "output": None}
        _store_llm_run(project_id, task, config, digest, result, "skipped", 0, "")
        return result
    started = time.time()
    body = {
        "model": config["model"],
        "temperature": float(env("LLM_TEMPERATURE", "0.2") or 0.2),
        "max_tokens": int(env("LLM_MAX_TOKENS", "1800") or 1800),
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": _dump(payload)[:24000]},
        ],
    }
    url = config["base_url"] + "/chat/completions"
    try:
        response = requests.post(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, json={**body, "response_format": {"type": "json_object"}}, timeout=45)
        if response.status_code in {400, 422}:
            response = requests.post(url, headers={"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}, json=body, timeout=45)
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"]
        usage = data.get("usage") or {}
        output = _extract_json(text)
        result = {"available": True, "reason": "", "output": output, "input_tokens": int(usage.get("prompt_tokens") or 0), "output_tokens": int(usage.get("completion_tokens") or 0)}
        _store_llm_run(project_id, task, config, digest, result, "ok", int((time.time() - started) * 1000), "")
        return result
    except (requests.RequestException, KeyError, IndexError, TypeError, ValueError) as error:
        result = {"available": False, "reason": "llm_request_failed", "output": None, "error": type(error).__name__}
        _store_llm_run(project_id, task, config, digest, result, "error", int((time.time() - started) * 1000), type(error).__name__)
        return result


def _extract_json(text: str) -> dict:
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except (TypeError, json.JSONDecodeError):
        pass
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise ValueError("llm_json")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("llm_json")
    return value


def _store_llm_run(project_id: str, task: str, config: dict, digest: str, result: dict, status: str, latency: int, error_code: str) -> None:
    execute(
        "INSERT INTO llm_runs (id, project_id, task_type, provider, model, prompt_version, input_hash, output, status, latency_ms, input_tokens, output_tokens, estimated_cost, error_code, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
        (new_id(), project_id, task, config.get("provider") or "", config.get("model") or "", PROMPT_VERSION, digest,
         _dump({"available": result.get("available"), "reason": result.get("reason"), "output": result.get("output")})[:65000],
         status, latency, int(result.get("input_tokens") or 0), int(result.get("output_tokens") or 0), error_code, now_iso()),
    )


def _assert_public(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("private_url")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as error:
        raise ValueError("unresolved_url") from error
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise ValueError("private_url")
    return url


def _normalize_url(raw: str) -> str:
    text = str(raw or "").strip()
    if text and "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.netloc:
        raise HTTPException(400, "Enter a public website URL.")
    clean = parsed._replace(fragment="", params="").geturl()
    try:
        return _assert_public(clean)
    except ValueError as error:
        raise HTTPException(400, "Private, local, or unresolvable URLs are not allowed.") from error


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.h1, self.links, self.jsonld = [], [], [], []
        self.meta_description = self.canonical = self.robots = self.language = ""
        self._skip = self._in_title = self._in_h1 = self._in_script = False
        self._script = []
        self._text = []

    def handle_starttag(self, tag, attrs):
        data = {key.lower(): value or "" for key, value in attrs}
        if tag in {"script", "style", "noscript"}:
            self._skip = True
            if tag == "script" and "ld+json" in data.get("type", ""):
                self._in_script = True
                self._script = []
        elif tag == "title":
            self._in_title = True
        elif tag == "h1" and not self.h1:
            self._in_h1 = True
        elif tag == "a" and data.get("href"):
            self.links.append(data["href"])
        elif tag == "meta":
            name = (data.get("name") or data.get("property") or "").lower()
            if name == "description" and not self.meta_description:
                self.meta_description = data.get("content", "")[:500]
            if name == "robots":
                self.robots = data.get("content", "")
        elif tag == "link" and data.get("rel", "").lower() == "canonical":
            self.canonical = data.get("href", "")
        elif tag == "html":
            self.language = (data.get("lang") or "")[:20]

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            if self._in_script:
                self.jsonld.append("".join(self._script))
            self._in_script = self._skip = False
        elif tag == "title":
            self._in_title = False
        elif tag == "h1":
            self._in_h1 = False

    def handle_data(self, data):
        if self._in_script:
            self._script.append(data)
        if self._in_title:
            self.title.append(data)
        elif self._in_h1:
            self.h1.append(data)
        elif not self._skip:
            self._text.append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self._text)).strip()


def _fetch(url: str) -> requests.Response:
    current = url
    for _ in range(4):
        _assert_public(current)
        response = requests.get(current, allow_redirects=False, timeout=FETCH_TIMEOUT, headers={"User-Agent": USER_AGENT}, stream=True)
        if response.status_code in {301, 302, 303, 307, 308}:
            location = response.headers.get("location")
            response.close()
            if not location:
                return response
            current = urljoin(current, location)
            continue
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_BYTES:
                break
            chunks.append(chunk)
        response._content = b"".join(chunks)
        response.url = current
        return response
    raise ValueError("redirect_limit")


def _schema_types(blocks: list[str]) -> list[str]:
    found = []
    for block in blocks:
        try:
            payload = json.loads(block)
        except (TypeError, json.JSONDecodeError):
            continue
        nodes = payload if isinstance(payload, list) else [payload]
        for node in nodes:
            if isinstance(node, dict):
                kind = node.get("@type")
                if isinstance(kind, list):
                    found.extend(str(item) for item in kind)
                elif kind:
                    found.append(str(kind))
    return sorted(set(found))[:20]


def _same_site(seed: str, url: str) -> bool:
    left, right = urlparse(seed).hostname or "", urlparse(url).hostname or ""
    return left.lower().removeprefix("www.") == right.lower().removeprefix("www.")


def _crawl(project: dict, run_id: str) -> dict:
    seed = project["website_url"]
    version = int((one("SELECT COUNT(*) AS n FROM website_crawls WHERE project_id = ?", (project["id"],)) or {}).get("n") or 0) + 1
    crawl_id = new_id()
    execute(
        "INSERT INTO website_crawls (id, project_id, run_id, started_at, status, crawl_version) VALUES (?, ?, ?, ?, 'running', ?)",
        (crawl_id, project["id"], run_id, now_iso(), version),
    )
    robots = RobotFileParser()
    robots_status = "missing"
    try:
        robots_url = urljoin(seed, "/robots.txt")
        response = _fetch(robots_url)
        if response.status_code == 200 and "html" not in response.headers.get("content-type", ""):
            robots.parse(response.text.splitlines())
            robots_status = "ok" if not _robots_blocks_all(response.text) else "block_all"
        elif response.status_code == 404:
            robots_status = "missing"
        else:
            robots_status = f"http_{response.status_code}"
    except (requests.RequestException, ValueError):
        robots_status = "unavailable"
    sitemap_status = "missing"
    queue = [seed]
    seen = set()
    pages = []
    errors = 0
    try:
        sitemap = _fetch(urljoin(seed, "/sitemap.xml"))
        if sitemap.status_code == 200 and "<loc" in sitemap.text.lower():
            sitemap_status = "ok"
            for loc in re.findall(r"<loc>\s*([^<]+)\s*</loc>", sitemap.text, flags=re.I)[:MAX_PAGES]:
                queue.append(loc.strip())
        elif sitemap.status_code != 404:
            sitemap_status = f"http_{sitemap.status_code}"
    except (requests.RequestException, ValueError):
        sitemap_status = "unavailable"
    while queue and len(pages) < MAX_PAGES:
        url = urldefrag(queue.pop(0))[0]
        if not url or url in seen or not _same_site(seed, url):
            continue
        seen.add(url)
        allowed = True
        try:
            allowed = robots.can_fetch(USER_AGENT, url) if robots_status == "ok" else robots_status != "block_all"
        except Exception:
            allowed = True
        page_id = new_id()
        try:
            started = time.time()
            response = _fetch(url)
            load_time = round(time.time() - started, 3)
            ctype = response.headers.get("content-type", "")
            parser = _Parser()
            if "html" in ctype or not ctype:
                try:
                    parser.feed(response.text)
                except Exception:
                    parser = _Parser()
            text = parser.text[:20000]
            title = " ".join(parser.title).strip()[:500]
            h1 = " ".join(parser.h1).strip()[:500]
            schema = _schema_types(parser.jsonld)
            canonical = urljoin(url, parser.canonical) if parser.canonical else ""
            indexable = response.status_code == 200 and allowed and "noindex" not in parser.robots.lower()
            execute(
                "INSERT INTO website_pages (id, crawl_id, url, status_code, content_hash, title, meta_description, canonical_url, h1, word_count, language, indexable, robots_allowed, load_time, schema_types, content) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (page_id, crawl_id, url[:1000], response.status_code, hashlib.sha256(text.encode()).hexdigest(), title, parser.meta_description,
                 canonical[:1000], h1, len(text.split()), parser.language, int(indexable), int(allowed), load_time, ",".join(schema), text[:12000]),
            )
            pages.append({"id": page_id, "url": url, "status_code": response.status_code, "title": title, "meta_description": parser.meta_description,
                          "canonical_url": canonical, "h1": h1, "word_count": len(text.split()), "indexable": indexable, "robots_allowed": allowed,
                          "load_time": load_time, "schema_types": schema, "content": text[:1500]})
            if response.status_code >= 400:
                errors += 1
            for href in parser.links:
                joined = urldefrag(urljoin(url, href))[0]
                if joined.startswith("http") and joined not in seen and _same_site(seed, joined):
                    queue.append(joined)
        except (requests.RequestException, ValueError):
            errors += 1
            execute(
                "INSERT INTO website_pages (id, crawl_id, url, status_code, indexable, robots_allowed, content) VALUES (?, ?, ?, 0, 0, ?, '')",
                (page_id, crawl_id, url[:1000], int(allowed)),
            )
    execute(
        "UPDATE website_crawls SET completed_at = ?, status = 'completed', pages_discovered = ?, pages_crawled = ?, error_count = ?, robots_status = ?, sitemap_status = ? WHERE id = ?",
        (now_iso(), len(seen), len(pages), errors, robots_status, sitemap_status, crawl_id),
    )
    return {"id": crawl_id, "pages": pages, "robots_status": robots_status, "sitemap_status": sitemap_status, "errors": errors}


def _robots_blocks_all(text: str) -> bool:
    agent = None
    for line in text.splitlines():
        item = line.split("#", 1)[0].strip()
        if not item or ":" not in item:
            continue
        key, value = [part.strip().lower() for part in item.split(":", 1)]
        if key == "user-agent":
            agent = value
        elif key == "disallow" and agent in {"*", user_agent_lower()} and value == "/":
            return True
    return False


def user_agent_lower() -> str:
    return USER_AGENT.lower()


def _finding(project_id: str, crawl_id: str, page_id: str, category: str, severity: str, code: str, title: str, description: str, evidence: str) -> str:
    finding_id = new_id()
    weight = _weights().get(code, 40)
    execute(
        "INSERT INTO seo_findings (id, project_id, crawl_id, page_id, category, severity, issue_code, title, description, evidence, impact_score, confidence_score, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 'open', ?)",
        (finding_id, project_id, crawl_id, page_id, category, severity, code, title, description, evidence[:1000], weight, now_iso()),
    )
    return finding_id


def _run_rules(project: dict, crawl: dict) -> list[dict]:
    findings = []
    titles: dict[str, list[str]] = {}
    schema_seen = False
    for page in crawl["pages"]:
        titles.setdefault(page.get("title") or "", []).append(page["id"])
        if page.get("schema_types"):
            schema_seen = True
        if page.get("status_code", 0) >= 400 or page.get("status_code", 0) == 0:
            findings.append(_finding(project["id"], crawl["id"], page["id"], "technical", "high", "http_error", "Page did not return a usable response", "The URL failed or returned an error status.", page["url"]))
            continue
        if not page.get("title"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "metadata", "high", "missing_title", "Missing title", "The page has no title element.", page["url"]))
        elif len(page["title"]) < 15:
            findings.append(_finding(project["id"], crawl["id"], page["id"], "metadata", "medium", "short_title", "Title is too short to describe the page", "Title length is under 15 characters.", page["title"]))
        if not page.get("meta_description"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "metadata", "medium", "missing_meta_description", "Missing meta description", "The page has no meta description.", page["url"]))
        if not page.get("h1"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "content", "medium", "missing_h1", "Missing H1", "The page has no H1.", page["url"]))
        if page.get("word_count", 0) < 80 and page.get("indexable"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "content", "medium", "thin_content", "Thin indexable content", "The indexable page has under 80 words of extracted text.", f"{page['url']} ({page.get('word_count', 0)} words)"))
        if not page.get("canonical_url"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "indexing", "medium", "missing_canonical", "Missing canonical", "The page does not declare a canonical URL.", page["url"]))
        elif urlparse(page["canonical_url"]).netloc and not _same_site(project["website_url"], page["canonical_url"]):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "indexing", "high", "canonical_mismatch", "Canonical points off-site", "The canonical host does not match the site.", page["canonical_url"]))
        if not page.get("indexable") and page.get("status_code") == 200:
            findings.append(_finding(project["id"], crawl["id"], page["id"], "indexing", "high", "noindex", "Page is marked noindex or blocked", "A 200 page is not indexable.", page["url"]))
        if page.get("load_time", 0) > 3:
            findings.append(_finding(project["id"], crawl["id"], page["id"], "technical", "medium", "slow_response", "Slow response", "Server response exceeded 3 seconds.", f"{page['load_time']}s"))
        if not page.get("schema_types"):
            findings.append(_finding(project["id"], crawl["id"], page["id"], "geo", "low", "missing_schema", "No structured data detected", "No JSON-LD type was found on this page.", page["url"]))
    for title, page_ids in titles.items():
        if title and len(page_ids) > 1:
            findings.append(_finding(project["id"], crawl["id"], page_ids[0], "metadata", "medium", "duplicate_title", "Duplicate title", "The same title is used on more than one crawled page.", title))
    if crawl["sitemap_status"] == "missing":
        findings.append(_finding(project["id"], crawl["id"], "", "indexing", "medium", "missing_sitemap", "Sitemap was not found", "GET /sitemap.xml did not return a sitemap.", "/sitemap.xml"))
    if crawl["robots_status"] == "block_all":
        findings.append(_finding(project["id"], crawl["id"], "", "indexing", "high", "robots_block", "robots.txt blocks the site", "A catch-all disallow was found.", "/robots.txt"))
    if not schema_seen:
        findings.append(_finding(project["id"], crawl["id"], "", "geo", "medium", "missing_entity_clarity", "Organization is not machine-readable", "No JSON-LD entity was detected on crawled pages.", project["domain"]))
    return findings


def _deterministic_profile(project: dict, run_id: str, crawl: dict) -> None:
    home = crawl["pages"][0] if crawl["pages"] else {}
    name = project.get("business_name") or home.get("title") or project["domain"]
    description = project.get("description") or home.get("meta_description") or ""
    execute(
        "INSERT INTO business_profiles (id, project_id, run_id, company_name, industry, business_type, description, products, services, audience, locations, countries, confidence_score, source, evidence, created_at) VALUES (?, ?, ?, ?, ?, '', ?, '[]', '[]', ?, ?, ?, 0.3, 'deterministic', ?, ?)",
        (new_id(), project["id"], run_id, name[:255], project.get("industry") or "", description[:2000],
         _dump([project.get("target_audience")] if project.get("target_audience") else []),
         project.get("target_locations") or "[]", _dump([project.get("target_country")] if project.get("target_country") else []),
         home.get("url") or project["website_url"], now_iso()),
    )
    execute(
        "INSERT INTO search_entities (id, project_id, run_id, entity_type, entity_name, description, source, confidence) VALUES (?, ?, ?, 'organization', ?, ?, 'deterministic', 0.3)",
        (new_id(), project["id"], run_id, name[:255], description[:500]),
    )
    for keyword in _json(project.get("keywords_seed"), []):
        text = str(keyword).strip()
        if text:
            execute(
                "INSERT INTO search_queries (id, project_id, run_id, query, query_type, intent, commercial_value, relevance_score, discovered_from, status) VALUES (?, ?, ?, ?, 'commercial', 'commercial', 50, 50, 'user', 'active')",
                (new_id(), project["id"], run_id, text[:500]),
            )
    for domain in _json(project.get("competitors_seed"), []):
        host = urlparse(str(domain) if "://" in str(domain) else f"https://{domain}").hostname
        if host:
            execute(
                "INSERT INTO competitors (id, project_id, run_id, domain, name, type, confidence, discovery_source, status, reason) VALUES (?, ?, ?, ?, '', 'direct', 1, 'user', 'active', 'Supplied by the user.')",
                (new_id(), project["id"], run_id, host[:255]),
            )
    label = name or project["domain"]
    for question in (f"What is {label}?", f"Who is {label} for?", f"{label} alternatives"):
        execute(
            "INSERT INTO aeo_questions (id, project_id, run_id, query, intent, priority, source, status) VALUES (?, ?, ?, ?, 'ai_question', 40, 'deterministic', 'unmeasured')",
            (new_id(), project["id"], run_id, question[:500]),
        )
    execute(
        "INSERT INTO geo_findings (id, project_id, run_id, code, title, description, evidence, score, confidence, status) VALUES (?, ?, ?, 'ai_visibility_unmeasured', 'AI visibility is not measured yet', 'No answer-engine observations have been recorded. This is not a score of zero.', '', 0, 1, 'open')",
        (new_id(), project["id"], run_id),
    )


def _apply_llm(project: dict, run_id: str, crawl: dict, finding_ids: list[str]) -> str:
    config = llm_settings(project["workspace_id"])
    if not config["enabled"]:
        return "awaiting_llm"
    evidence = {
        "domain": project["domain"],
        "user_context": {"business_name": project.get("business_name"), "industry": project.get("industry"), "description": project.get("description"), "country": project.get("target_country")},
        "pages": [{key: page.get(key) for key in ("url", "title", "meta_description", "h1", "word_count", "schema_types", "indexable")} for page in crawl["pages"][:12]],
        "findings": query("SELECT issue_code, severity, title, evidence FROM seo_findings WHERE project_id = ? AND crawl_id = ? LIMIT 40", (project["id"], crawl["id"])),
    }
    result = run_llm("search_plan", SYSTEM_PROMPT + "\n" + TASK_PLAN, evidence, None, project["workspace_id"], project["id"])
    output = result.get("output") if result.get("available") else None
    if not isinstance(output, dict):
        return "awaiting_llm"
    business = output.get("business") if isinstance(output.get("business"), dict) else {}
    if business.get("company_name") or business.get("description"):
        execute(
            "INSERT INTO business_profiles (id, project_id, run_id, company_name, industry, business_type, description, products, services, audience, locations, countries, confidence_score, source, evidence, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'llm', ?, ?)",
            (new_id(), project["id"], run_id, str(business.get("company_name") or "")[:255], str(business.get("industry") or "")[:120], str(business.get("business_type") or "")[:120],
             str(business.get("description") or "")[:2000], _dump(business.get("products") or []), _dump(business.get("services") or []), _dump(business.get("audience") or []),
             _dump(business.get("locations") or []), _dump(business.get("countries") or []), _clamp(business.get("confidence"), 0, 1, 0.4), str(business.get("evidence") or "")[:1000], now_iso()),
        )
    for entity in (output.get("entities") or [])[:20]:
        if isinstance(entity, dict) and entity.get("entity_name"):
            execute("INSERT INTO search_entities (id, project_id, run_id, entity_type, entity_name, description, source, confidence) VALUES (?, ?, ?, ?, ?, ?, 'llm', ?)",
                    (new_id(), project["id"], run_id, str(entity.get("entity_type") or "organization")[:40], str(entity["entity_name"])[:255], str(entity.get("description") or "")[:500], _clamp(entity.get("confidence"), 0, 1, 0.4)))
    for topic in (output.get("topics") or [])[:20]:
        if isinstance(topic, dict) and topic.get("topic"):
            execute("INSERT INTO search_topics (id, project_id, run_id, topic, intent, importance, relevance_score, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'llm')",
                    (new_id(), project["id"], run_id, str(topic["topic"])[:255], str(topic.get("intent") or "")[:40], _clamp(topic.get("importance"), 0, 100), _clamp(topic.get("relevance_score"), 0, 100), _clamp(topic.get("confidence"), 0, 1, 0.4)))
    for item in (output.get("queries") or [])[:30]:
        if isinstance(item, dict) and item.get("query"):
            execute("INSERT INTO search_queries (id, project_id, run_id, query, query_type, intent, commercial_value, relevance_score, discovered_from, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'llm', 'hypothesis')",
                    (new_id(), project["id"], run_id, str(item["query"])[:500], str(item.get("query_type") or "informational")[:40], str(item.get("intent") or "")[:40], _clamp(item.get("commercial_value"), 0, 100), _clamp(item.get("relevance_score"), 0, 100)))
    for item in (output.get("competitors") or [])[:15]:
        if isinstance(item, dict) and item.get("domain"):
            host = urlparse(str(item["domain"]) if "://" in str(item["domain"]) else f"https://{item['domain']}").hostname
            if host and host != project["domain"]:
                execute("INSERT INTO competitors (id, project_id, run_id, domain, name, type, confidence, discovery_source, status, reason) VALUES (?, ?, ?, ?, ?, ?, ?, 'llm', 'hypothesis', ?)",
                        (new_id(), project["id"], run_id, host[:255], str(item.get("name") or "")[:255], str(item.get("type") or "serp")[:40], _clamp(item.get("confidence"), 0, 1, 0.3), str(item.get("reason") or "")[:500]))
    _llm_plan(project, run_id, output, finding_ids)
    return "completed"


def _llm_plan(project: dict, run_id: str, output: dict, finding_ids: list[str]) -> None:
    known = {row["issue_code"]: row["id"] for row in query("SELECT id, issue_code FROM seo_findings WHERE project_id = ? ORDER BY created_at DESC LIMIT 80", (project["id"],))}
    for item in (output.get("opportunities") or [])[:12]:
        if not isinstance(item, dict) or not item.get("title"):
            continue
        codes = [code for code in item.get("finding_codes") or [] if code in known]
        if not codes:
            continue
        opportunity_id = new_id()
        impact, confidence, effort = _clamp(item.get("impact_score"), 0, 100, 40), _clamp(item.get("confidence_score"), 0, 1, 0.4), _clamp(item.get("effort_score"), 1, 5, 3)
        execute(
            "INSERT INTO growth_opportunities (id, project_id, run_id, type, title, description, root_cause, evidence, impact_score, confidence_score, effort_score, priority_score, affected_pages, affected_queries, affected_competitors, finding_ids, source, status, created_at) VALUES (?, ?, ?, 'seo', ?, ?, ?, '', ?, ?, ?, ?, '[]', '[]', '[]', ?, 'llm', 'open', ?)",
            (opportunity_id, project["id"], run_id, str(item["title"])[:255], str(item.get("description") or "")[:2000], str(item.get("root_cause") or "relevance")[:60],
             impact, confidence, effort, round(impact * confidence / effort, 2), _dump([known[code] for code in codes]), now_iso()),
        )
    for item in (output.get("actions") or [])[:20]:
        if not isinstance(item, dict) or not item.get("title") or not item.get("opportunity_title"):
            continue
        opportunity = one("SELECT id FROM growth_opportunities WHERE project_id = ? AND run_id = ? AND title = ? AND source = 'llm'", (project["id"], run_id, str(item["opportunity_title"])[:255]))
        if not opportunity:
            continue
        classification = item.get("classification") if item.get("classification") in {"SAFE_AUTOMATION", "USER_APPROVAL", "MANUAL_ACTION", "INSUFFICIENT_DATA"} else "USER_APPROVAL"
        if classification == "SAFE_AUTOMATION" and item.get("action_type") not in {"metadata_title", "metadata_description", "schema_organization", "robots_txt", "sitemap", "canonical"}:
            classification = "USER_APPROVAL"
        execute(
            "INSERT INTO growth_actions (id, opportunity_id, project_id, action_type, title, description, expected_impact, effort, automatable, requires_user, classification, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, 'proposed', ?)",
            (new_id(), opportunity["id"], project["id"], str(item.get("action_type") or "review")[:60], str(item["title"])[:255], str(item.get("description") or "")[:2000],
             str(item.get("expected_impact") or "")[:255], _clamp(item.get("effort"), 1, 5, 3), int(classification == "SAFE_AUTOMATION"), classification, now_iso()),
        )


def _plan_from_rules(project: dict, run_id: str, crawl_id: str) -> None:
    grouped: dict[str, list[dict]] = {}
    for row in query("SELECT * FROM seo_findings WHERE project_id = ? AND crawl_id = ?", (project["id"], crawl_id)):
        grouped.setdefault(row["issue_code"], []).append(row)
    weights = _weights()
    for code, rows in grouped.items():
        action = ISSUE_ACTIONS.get(code)
        if not action:
            continue
        action_type, title, classification, effort = action
        impact = weights.get(code, 40)
        confidence = 0.9
        opportunity_id = new_id()
        execute(
            "INSERT INTO growth_opportunities (id, project_id, run_id, type, title, description, root_cause, evidence, impact_score, confidence_score, effort_score, priority_score, affected_pages, affected_queries, affected_competitors, finding_ids, source, status, created_at) VALUES (?, ?, ?, 'technical', ?, ?, 'technical_accessibility', ?, ?, ?, ?, ?, ?, '[]', '[]', ?, 'rules', 'open', ?)",
            (opportunity_id, project["id"], run_id, title, f"{len(rows)} crawled URL(s) matched {code}.", rows[0].get("evidence") or "", impact, confidence, effort,
             round(impact * confidence / effort, 2), _dump([row.get("page_id") for row in rows if row.get("page_id")]), _dump([row["id"] for row in rows]), now_iso()),
        )
        execute(
            "INSERT INTO growth_actions (id, opportunity_id, project_id, action_type, title, description, expected_impact, effort, automatable, requires_user, classification, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'Measured on the next crawl. No ranking is promised.', ?, ?, 1, ?, 'proposed', ?)",
            (new_id(), opportunity_id, project["id"], action_type, title, rows[0].get("description") or "", effort, int(classification == "SAFE_AUTOMATION"), classification, now_iso()),
        )


def _set_phase(run_id: str, phase: str, progress: dict, llm_status: str = "") -> None:
    execute("UPDATE search_analysis_runs SET status = ?, phase = ?, progress = ?, llm_status = ?, updated_at = ? WHERE id = ?",
            (phase, phase, _dump(progress), llm_status, now_iso(), run_id))


def _execute_run(run: dict) -> None:
    project = one("SELECT * FROM search_projects WHERE id = ?", (run["project_id"],))
    if not project:
        raise RuntimeError("project_missing")
    _set_phase(run["id"], "crawling", {"step": "crawling"})
    crawl = _crawl(project, run["id"])
    _set_phase(run["id"], "analyzing", {"pages_crawled": len(crawl["pages"]), "errors": crawl["errors"]})
    _run_rules(project, crawl)
    _deterministic_profile(project, run["id"], crawl)
    _set_phase(run["id"], "intelligence", {"pages_crawled": len(crawl["pages"])})
    llm_status = _apply_llm(project, run["id"], crawl, [])
    _set_phase(run["id"], "planning", {"pages_crawled": len(crawl["pages"])}, llm_status)
    _plan_from_rules(project, run["id"], crawl["id"])
    execute(
        "UPDATE search_analysis_runs SET status = 'completed', phase = 'completed', llm_status = ?, progress = ?, completed_at = ?, updated_at = ? WHERE id = ?",
        (llm_status, _dump({"pages_crawled": len(crawl["pages"]), "errors": crawl["errors"], "robots": crawl["robots_status"], "sitemap": crawl["sitemap_status"]}), now_iso(), now_iso(), run["id"]),
    )
    execute("UPDATE search_projects SET last_analyzed_at = ?, updated_at = ?, status = 'active' WHERE id = ?", (now_iso(), now_iso(), project["id"]))


def _fail_run(run: dict, error: Exception) -> None:
    execute("UPDATE search_analysis_runs SET status = 'failed', phase = 'failed', error = ?, updated_at = ?, completed_at = ? WHERE id = ?",
            (str(error)[:500], now_iso(), now_iso(), run["id"]))


def _queue_due_projects() -> None:
    cutoff = now_iso()[:10]
    rows = query(
        "SELECT id, workspace_id FROM search_projects WHERE status = 'active' AND (last_analyzed_at = '' OR last_analyzed_at < ?) "
        "AND id NOT IN (SELECT project_id FROM search_analysis_runs WHERE status IN ('queued', 'crawling', 'analyzing', 'intelligence', 'planning')) LIMIT 1",
        (cutoff,),
    )
    for row in rows:
        if not one("SELECT id FROM search_analysis_runs WHERE project_id = ? AND created_at LIKE ?", (row["id"], cutoff + "%")):
            execute(
                "INSERT INTO search_analysis_runs (id, workspace_id, project_id, status, phase, progress, llm_status, created_at, updated_at) VALUES (?, ?, ?, 'queued', 'queued', ?, '', ?, ?)",
                (new_id(), row["workspace_id"], row["id"], _dump({"reason": "daily"}), now_iso(), now_iso()),
            )


def process_search_runs(limit: int = 1) -> dict:
    ensure_tables()
    _queue_due_projects()
    runs = query("SELECT * FROM search_analysis_runs WHERE status = 'queued' ORDER BY created_at ASC LIMIT ?", (limit,))
    processed = failed = 0
    for run in runs:
        execute("UPDATE search_analysis_runs SET status = 'discovering', phase = 'discovering', updated_at = ? WHERE id = ? AND status = 'queued'", (now_iso(), run["id"]))
        claimed = one("SELECT status FROM search_analysis_runs WHERE id = ?", (run["id"],))
        if not claimed or claimed["status"] != "discovering":
            continue
        try:
            _execute_run(run)
            processed += 1
        except Exception as error:
            _fail_run(run, error)
            failed += 1
    return {"processed": processed, "failed": failed, "scanned": len(runs)}


def _queue_run(project: dict) -> str:
    run_id = new_id()
    execute(
        "INSERT INTO search_analysis_runs (id, workspace_id, project_id, status, phase, progress, llm_status, created_at, updated_at) VALUES (?, ?, ?, 'queued', 'queued', '{}', '', ?, ?)",
        (run_id, project["workspace_id"], project["id"], now_iso(), now_iso()),
    )
    return run_id


@router.post("/projects")
def create_project(payload: dict, user: dict = Depends(user_from_header)) -> dict:
    ensure_tables()
    website = _normalize_url(payload.get("website_url") or "")
    domain = (urlparse(website).hostname or "").lower().removeprefix("www.")
    project_id = new_id()
    execute(
        "INSERT INTO search_projects (id, workspace_id, website_url, domain, business_name, industry, description, target_country, target_locations, competitors_seed, keywords_seed, target_audience, settings, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
        (project_id, user["workspace_id"], website, domain, str(payload.get("business_name") or "")[:255], str(payload.get("industry") or "")[:120],
         str(payload.get("business_description") or "")[:4000], str(payload.get("target_country") or "")[:80], _dump(payload.get("target_locations") or []),
         _dump(payload.get("competitors") or []), _dump(payload.get("keywords") or []), str(payload.get("target_audience") or "")[:1000],
         _dump({"integrations": payload.get("integrations") or {}}), now_iso(), now_iso()),
    )
    run_id = _queue_run({"id": project_id, "workspace_id": user["workspace_id"]})
    return {"id": project_id, "run_id": run_id, "status": "queued"}


@router.get("/projects")
def list_projects(user: dict = Depends(user_from_header)) -> list[dict]:
    ensure_tables()
    return query("SELECT id, website_url, domain, business_name, industry, status, last_analyzed_at, created_at FROM search_projects WHERE workspace_id = ? ORDER BY created_at DESC", (user["workspace_id"],))


@router.get("/projects/{project_id}")
def get_project(project_id: str, user: dict = Depends(user_from_header)) -> dict:
    project = _project(user["workspace_id"], project_id)
    run = one("SELECT * FROM search_analysis_runs WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,))
    profile = one("SELECT * FROM business_profiles WHERE project_id = ? ORDER BY created_at DESC LIMIT 1", (project_id,))
    counts = {
        "findings": (one("SELECT COUNT(*) AS n FROM seo_findings WHERE project_id = ?", (project_id,)) or {}).get("n", 0),
        "opportunities": (one("SELECT COUNT(*) AS n FROM growth_opportunities WHERE project_id = ?", (project_id,)) or {}).get("n", 0),
        "actions": (one("SELECT COUNT(*) AS n FROM growth_actions WHERE project_id = ?", (project_id,)) or {}).get("n", 0),
    }
    if run:
        run["progress"] = _json(run.get("progress"), {})
    return {"project": project, "run": run, "business": profile, "counts": counts, "llm": {k: v for k, v in llm_settings(user["workspace_id"]).items() if k != "api_key"}}


@router.post("/projects/{project_id}/analyze")
def analyze_project(project_id: str, user: dict = Depends(user_from_header)) -> dict:
    project = _project(user["workspace_id"], project_id)
    active = one("SELECT id FROM search_analysis_runs WHERE project_id = ? AND status IN ('queued', 'discovering', 'crawling', 'analyzing', 'intelligence', 'planning')", (project_id,))
    if active:
        return {"id": project_id, "run_id": active["id"], "status": "already_queued"}
    return {"id": project_id, "run_id": _queue_run(project), "status": "queued"}


@router.get("/runs/{run_id}")
def get_run(run_id: str, user: dict = Depends(user_from_header)) -> dict:
    run = one("SELECT * FROM search_analysis_runs WHERE id = ? AND workspace_id = ?", (run_id, user["workspace_id"]))
    if not run:
        raise HTTPException(404, "Analysis run not found.")
    run["progress"] = _json(run.get("progress"), {})
    return run


@router.get("/projects/{project_id}/findings")
def list_findings(project_id: str, user: dict = Depends(user_from_header)) -> dict:
    _project(user["workspace_id"], project_id)
    return {
        "seo": query("SELECT * FROM seo_findings WHERE project_id = ? ORDER BY impact_score DESC LIMIT 200", (project_id,)),
        "geo": query("SELECT * FROM geo_findings WHERE project_id = ? LIMIT 50", (project_id,)),
        "aeo_questions": query("SELECT * FROM aeo_questions WHERE project_id = ? LIMIT 50", (project_id,)),
    }


@router.get("/projects/{project_id}/opportunities")
def list_opportunities(project_id: str, user: dict = Depends(user_from_header)) -> list[dict]:
    _project(user["workspace_id"], project_id)
    return query("SELECT * FROM growth_opportunities WHERE project_id = ? ORDER BY priority_score DESC LIMIT 100", (project_id,))


@router.get("/projects/{project_id}/actions")
def list_actions(project_id: str, user: dict = Depends(user_from_header)) -> list[dict]:
    _project(user["workspace_id"], project_id)
    return query("SELECT * FROM growth_actions WHERE project_id = ? ORDER BY created_at DESC LIMIT 150", (project_id,))


@router.post("/actions/{action_id}/feedback")
def action_feedback(action_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    action = one("SELECT a.*, p.workspace_id FROM growth_actions a JOIN search_projects p ON p.id = a.project_id WHERE a.id = ? AND p.workspace_id = ?", (action_id, user["workspace_id"]))
    if not action:
        raise HTTPException(404, "Action not found.")
    kind = str(payload.get("feedback_type") or "")
    if kind not in {"USER_APPROVED", "USER_REJECTED", "USER_MARKED_USEFUL", "USER_MARKED_NOT_USEFUL", "ALREADY_FIXED", "NOT_APPLICABLE"}:
        raise HTTPException(400, "Unknown feedback type.")
    execute("INSERT INTO feedback_events (id, project_id, action_id, feedback_type, rating, feedback_text, source, created_at) VALUES (?, ?, ?, ?, ?, ?, 'user', ?)",
            (new_id(), action["project_id"], action_id, kind, payload.get("rating"), str(payload.get("feedback_text") or "")[:2000], now_iso()))
    if kind == "USER_REJECTED":
        execute("UPDATE growth_actions SET status = 'rejected' WHERE id = ?", (action_id,))
    elif kind == "USER_APPROVED":
        execute("UPDATE growth_actions SET status = 'approved' WHERE id = ?", (action_id,))
    return {"ok": True}


@router.post("/actions/{action_id}/outcomes")
def record_outcome(action_id: str, payload: dict, user: dict = Depends(user_from_header)) -> dict:
    action = one(
        "SELECT a.*, p.workspace_id, p.industry FROM growth_actions a JOIN search_projects p ON p.id = a.project_id WHERE a.id = ? AND p.workspace_id = ?",
        (action_id, user["workspace_id"]),
    )
    if not action:
        raise HTTPException(404, "Action not found.")
    metric = str(payload.get("metric") or "").strip()[:60]
    if not metric:
        raise HTTPException(400, "A metric name is required.")
    baseline, actual = payload.get("baseline_value"), payload.get("actual_value")
    change = None
    outcome = "unmeasured"
    try:
        if baseline not in (None, "") and actual not in (None, "") and float(baseline) != 0:
            change = round((float(actual) - float(baseline)) / abs(float(baseline)) * 100, 2)
            outcome = "improved" if float(actual) > float(baseline) else "unchanged" if float(actual) == float(baseline) else "regressed"
    except (TypeError, ValueError):
        raise HTTPException(400, "Baseline and actual values must be numeric.")
    execute(
        "INSERT INTO action_outcomes (id, action_id, project_id, metric, baseline_value, target_value, actual_value, change_pct, measurement_window, outcome, confidence, measured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (new_id(), action_id, action["project_id"], metric, baseline, payload.get("target_value"), actual, change, str(payload.get("measurement_window") or "")[:40], outcome, 0.7 if change is not None else 0.3, now_iso()),
    )
    if outcome in {"improved", "regressed", "unchanged"}:
        execute(
            "INSERT INTO learning_observations (id, project_id, action_type, industry, business_type, condition_key, action, outcome, success_score, created_at) VALUES (?, ?, ?, ?, '', ?, ?, ?, ?, ?)",
            (new_id(), action["project_id"], action["action_type"], action.get("industry") or "", action["action_type"], action["title"], outcome, 1 if outcome == "improved" else 0, now_iso()),
        )
        _refresh_patterns(action["action_type"], action.get("industry") or "")
    return {"ok": True, "outcome": outcome, "change_pct": change}


def _refresh_patterns(action_type: str, industry: str) -> None:
    rows = query("SELECT outcome, success_score FROM learning_observations WHERE action_type = ? AND industry = ?", (action_type, industry))
    if len(rows) < MIN_PATTERN_SAMPLE:
        return
    success = sum(float(row["success_score"] or 0) for row in rows) / len(rows)
    status = "eligible" if len(rows) >= ACTIVATE_SAMPLE and success >= ACTIVATE_SUCCESS else "watching"
    execute(
        "INSERT INTO learning_patterns (id, pattern_type, segment, condition_key, recommended_action, success_rate, avg_impact, avg_effort, sample_size, confidence, status, updated_at) VALUES (?, 'action_effectiveness', ?, ?, ?, ?, 0, 0, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE success_rate = VALUES(success_rate), sample_size = VALUES(sample_size), confidence = VALUES(confidence), status = VALUES(status), updated_at = VALUES(updated_at)",
        (new_id(), industry or "all", action_type, action_type, round(success, 3), len(rows), round(min(0.95, len(rows) / 50), 3), status, now_iso()),
    )
    if status == "eligible" and env_flag("SEARCH_LEARNING_AUTO_ACTIVATE"):
        weights = _weights()
        weights[action_type] = round(min(95, weights.get(action_type, 50) * (0.8 + success)), 2)
        version = int((one("SELECT MAX(version) AS v FROM strategy_versions WHERE strategy_name = 'issue_weights'") or {}).get("v") or 1) + 1
        execute("UPDATE strategy_versions SET active = 0 WHERE strategy_name = 'issue_weights'")
        execute("INSERT INTO strategy_versions (id, strategy_name, version, parameters, reason, active, status, created_at) VALUES (?, 'issue_weights', ?, ?, ?, 1, 'active', ?)",
                (new_id(), version, _dump(weights), f"Auto-activated after {len(rows)} outcomes at {success:.0%} success.", now_iso()))


@router.post("/actions/{action_id}/execute")
def execute_action(action_id: str, user: dict = Depends(user_from_header)) -> dict:
    action = one("SELECT a.*, p.workspace_id, p.domain FROM growth_actions a JOIN search_projects p ON p.id = a.project_id WHERE a.id = ? AND p.workspace_id = ?", (action_id, user["workspace_id"]))
    if not action:
        raise HTTPException(404, "Action not found.")
    job_id = new_id()
    if action["classification"] in {"MANUAL_ACTION", "INSUFFICIENT_DATA"}:
        execute("INSERT INTO execution_jobs (id, action_id, project_id, execution_type, status, result, created_at, completed_at) VALUES (?, ?, ?, ?, 'needs_user', ?, ?, ?)",
                (job_id, action_id, action["project_id"], action["action_type"], _dump({"note": "Revenue360s cannot safely execute this change."}), now_iso(), now_iso()))
        return {"id": job_id, "status": "needs_user"}
    if action["action_type"] != "expand_content":
        execute("INSERT INTO execution_jobs (id, action_id, project_id, execution_type, status, result, created_at, completed_at) VALUES (?, ?, ?, ?, 'ready_for_review', ?, ?, ?)",
                (job_id, action_id, action["project_id"], action["action_type"], _dump({"plan": action["description"], "classification": action["classification"], "note": "Patch plan only. The live site was not modified."}), now_iso(), now_iso()))
        execute("UPDATE growth_actions SET status = 'planned' WHERE id = ?", (action_id,))
        return {"id": job_id, "status": "ready_for_review"}
    result = run_llm("generate_content", SYSTEM_PROMPT, {"domain": action["domain"], "action": action["title"], "instruction": action["description"], "rules": "Draft only. No invented metrics."}, None, user["workspace_id"], action["project_id"])
    status = "completed" if result.get("available") else "awaiting_llm"
    execute("INSERT INTO execution_jobs (id, action_id, project_id, execution_type, provider, status, result, created_at, completed_at) VALUES (?, ?, ?, 'generate_content', 'llm', ?, ?, ?, ?)",
            (job_id, action_id, action["project_id"], status, _dump(result.get("output") or {"available": False, "reason": result.get("reason")}), now_iso(), now_iso()))
    return {"id": job_id, "status": status}


@router.get("/learning/patterns")
def list_patterns(user: dict = Depends(user_from_header)) -> dict:
    del user
    ensure_tables()
    return {"patterns": query("SELECT * FROM learning_patterns ORDER BY sample_size DESC LIMIT 50"), "strategies": query("SELECT id, strategy_name, version, status, active, reason, created_at FROM strategy_versions ORDER BY created_at DESC LIMIT 20")}


@router.post("/strategy/{strategy_id}/activate")
def activate_strategy(strategy_id: str, user: dict = Depends(user_from_header)) -> dict:
    del user
    row = one("SELECT * FROM strategy_versions WHERE id = ?", (strategy_id,))
    if not row:
        raise HTTPException(404, "Strategy version not found.")
    execute("UPDATE strategy_versions SET active = 0 WHERE strategy_name = ?", (row["strategy_name"],))
    execute("UPDATE strategy_versions SET active = 1, status = 'active' WHERE id = ?", (strategy_id,))
    return {"ok": True}


@router.get("/llm")
def llm_status(user: dict = Depends(user_from_header)) -> dict:
    config = llm_settings(user["workspace_id"])
    return {"available": config["enabled"], "provider": config["provider"], "model": config["model"], "reason": "" if config["enabled"] else "llm_not_configured"}