from __future__ import annotations
 
import math
import random
import re
 
import hashlib
 
from db import env, execute, hours_from_now, insert, now_iso, one, query
 
WINDOW_H = float(env("R360_ATTRIBUTION_HOURS", "96") or 96)  # attribution window
PRIOR_STRENGTH = 4.0     # how much a parent context's rate counts as prior
HOLDOUT = 0.10           # share of decisions that use the baseline arm
MIN_N_GUARD = 30         # min settled sends before the unsubscribe guardrail can act
UNSUB_LIMIT = 0.03       # disable an arm above this unsubscribe rate
REWARD = {"booked": 1.0, "won": 1.0, "reply_positive": 1.0, "reply_neutral": 0.3, "reply_negative": 0.0, "click": 0.1}
GUARD = {"unsubscribe", "complaint"}
 
_TABLES = [
    """CREATE TABLE IF NOT EXISTS arm_stats (
        workspace_id VARCHAR(36) NOT NULL, flow VARCHAR(60) NOT NULL,
        context_key VARCHAR(120) NOT NULL, arm_key VARCHAR(120) NOT NULL,
        wins DOUBLE NOT NULL DEFAULT 0, losses DOUBLE NOT NULL DEFAULT 0,
        unsubs INT NOT NULL DEFAULT 0, disabled TINYINT NOT NULL DEFAULT 0,
        updated_at VARCHAR(40) NOT NULL DEFAULT '',
        PRIMARY KEY (workspace_id, flow, context_key, arm_key))""",
    """CREATE TABLE IF NOT EXISTS decisions (
        id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL,
        flow VARCHAR(60) NOT NULL, context_key VARCHAR(120) NOT NULL,
        arm_key VARCHAR(120) NOT NULL, lead_id VARCHAR(36) NOT NULL DEFAULT '',
        holdout TINYINT NOT NULL DEFAULT 0, settled TINYINT NOT NULL DEFAULT 0,
        reward DOUBLE NOT NULL DEFAULT 0, created_at VARCHAR(40) NOT NULL,
        INDEX idx_dec_lead (workspace_id, lead_id, settled),
        INDEX idx_dec_open (settled, created_at))""",
    """CREATE TABLE IF NOT EXISTS outcomes (
        id VARCHAR(36) PRIMARY KEY, workspace_id VARCHAR(36) NOT NULL,
        decision_id VARCHAR(36) NOT NULL, lead_id VARCHAR(36) NOT NULL DEFAULT '',
        kind VARCHAR(40) NOT NULL, created_at VARCHAR(40) NOT NULL,
        INDEX idx_out_dec (decision_id))""",
    """CREATE TABLE IF NOT EXISTS nb_tokens (
        workspace_id VARCHAR(36) NOT NULL, label VARCHAR(20) NOT NULL,
        token VARCHAR(60) NOT NULL, n INT NOT NULL DEFAULT 0,
        PRIMARY KEY (workspace_id, label, token))""",
]
 
 
def ensure_tables() -> None:
    for statement in _TABLES:
        execute(statement)
 
 
def variant_key(subject: str, body: str) -> str:
    """Stable arm id from content, so stats survive sequence re-saves (step rows are re-created on save)."""
    return hashlib.sha1(f"{subject}\n{body}".encode()).hexdigest()[:12]
 
 
def void_decision(decision_id: str) -> None:
    """Drop a decision whose send never went out."""
    execute("DELETE FROM decisions WHERE id = ? AND settled = 0", (decision_id,))
 
 
def record_stage_feedback(ws: str, lead_id: str, stage: str) -> None:
    """A human moved a lead's stage: attribute the win and teach the reply classifier."""
    if stage in ("booked", "won"):
        record_outcome(ws, lead_id, stage)
    label = "positive" if stage in ("booked", "won") else "negative" if stage == "lost" else ""
    if label:
        last = one(
            "SELECT body FROM messages WHERE workspace_id = ? AND lead_id = ? AND direction = 'inbound' ORDER BY created_at DESC LIMIT 1",
            (ws, lead_id),
        )
        if last and last.get("body"):
            train(ws, last["body"], label)
 
 
# ---------------------------------------------------------------- bandit
 
def _stats(ws: str, flow: str, ctx: str, arm: str) -> dict:
    row = one(
        "SELECT wins, losses, disabled FROM arm_stats WHERE workspace_id = ? AND flow = ? AND context_key = ? AND arm_key = ?",
        (ws, flow, ctx, arm),
    )
    return row or {"wins": 0.0, "losses": 0.0, "disabled": 0}
 
 
def choose(ws: str, flow: str, arms: list[str], context_key: str = "all", lead_id: str = "") -> tuple[str, str]:
    """Pick an arm. Returns (arm_key, decision_id). arms[0] is the baseline."""
    if not arms:
        raise ValueError("arms required")
    live = [a for a in arms if not _stats(ws, flow, context_key, a)["disabled"]] or list(arms)
    holdout = random.random() < HOLDOUT
    if holdout:
        arm = live[0]
    else:
        arm, best = live[0], -1.0
        for candidate in live:
            own = _stats(ws, flow, context_key, candidate)
            if context_key == "all":
                prior = 0.5
            else:  # back off to the flow-wide rate for this arm until the context has its own data
                parent = _stats(ws, flow, "all", candidate)
                prior = (parent["wins"] + 1) / (parent["wins"] + parent["losses"] + 2)
            draw = random.betavariate(
                own["wins"] + 1 + PRIOR_STRENGTH * prior,
                own["losses"] + 1 + PRIOR_STRENGTH * (1 - prior),
            )
            if draw > best:
                arm, best = candidate, draw
    decision_id = insert(
        "decisions",
        {"flow": flow, "context_key": context_key, "arm_key": arm, "lead_id": lead_id, "holdout": int(holdout)},
        ws,
    )
    return arm, decision_id
 
 
def record_outcome(ws: str, lead_id: str, kind: str) -> str | None:
    """Attribute an outcome to the lead's latest open decision inside the window."""
    if not lead_id or (kind not in REWARD and kind not in GUARD):
        return None
    decision = one(
        "SELECT id, flow, context_key, arm_key FROM decisions WHERE workspace_id = ? AND lead_id = ? AND settled = 0 AND created_at >= ? ORDER BY created_at DESC LIMIT 1",
        (ws, lead_id, hours_from_now(-WINDOW_H)),
    )
    if not decision:
        return None
    insert("outcomes", {"decision_id": decision["id"], "lead_id": lead_id, "kind": kind}, ws)
    if kind in GUARD:
        for ctx in {decision["context_key"], "all"}:
            _bump(ws, decision["flow"], ctx, decision["arm_key"], unsubs=1)
    return decision["id"]
 
 
def _bump(ws: str, flow: str, ctx: str, arm: str, wins: float = 0.0, losses: float = 0.0, unsubs: int = 0) -> None:
    execute(
        "INSERT INTO arm_stats (workspace_id, flow, context_key, arm_key, wins, losses, unsubs, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON DUPLICATE KEY UPDATE wins = wins + VALUES(wins), losses = losses + VALUES(losses), unsubs = unsubs + VALUES(unsubs), updated_at = VALUES(updated_at)",
        (ws, flow, ctx, arm, wins, losses, unsubs, now_iso()),
    )
 
 
def settle(limit: int = 500) -> dict:
    """Close decisions older than the window: outcomes -> wins/losses. Idempotent per decision."""
    due = query(
        "SELECT id, workspace_id, flow, context_key, arm_key FROM decisions WHERE settled = 0 AND created_at < ? LIMIT ?",
        (hours_from_now(-WINDOW_H), limit),
    )
    for d in due:
        kinds = {r["kind"] for r in query("SELECT kind FROM outcomes WHERE decision_id = ?", (d["id"],))}
        reward = max([REWARD[k] for k in kinds if k in REWARD] or [0.0])
        if kinds & GUARD:
            reward = 0.0
        for ctx in {d["context_key"], "all"}:
            _bump(d["workspace_id"], d["flow"], ctx, d["arm_key"], wins=reward, losses=1.0 - reward)
        execute("UPDATE decisions SET settled = 1, reward = ? WHERE id = ?", (reward, d["id"]))
    disabled = execute_guardrail()
    return {"settled": len(due), "arms_disabled": disabled}
 
 
def execute_guardrail() -> int:
    rows = query(
        "SELECT workspace_id, flow, context_key, arm_key, unsubs, wins, losses FROM arm_stats WHERE disabled = 0 AND unsubs >= 3"
    )
    count = 0
    for r in rows:
        n = r["wins"] + r["losses"]
        if n >= MIN_N_GUARD and r["unsubs"] / n > UNSUB_LIMIT:
            execute(
                "UPDATE arm_stats SET disabled = 1 WHERE workspace_id = ? AND flow = ? AND context_key = ? AND arm_key = ?",
                (r["workspace_id"], r["flow"], r["context_key"], r["arm_key"]),
            )
            count += 1
    return count
 
 
def report(ws: str, flow: str, arm_keys: list[str] | None = None) -> dict:
    """What the system has actually learned, plus measured lift of the learned policy vs always sending the baseline.
    `n` counts settled sends; `pending` counts sends still inside the attribution window (not yet learned from)."""
    arms = query(
        "SELECT arm_key, wins, losses, unsubs, disabled FROM arm_stats WHERE workspace_id = ? AND flow = ? AND context_key = 'all'",
        (ws, flow),
    )
    seen = {a["arm_key"] for a in arms}
    for key in arm_keys or []:
        if key not in seen:
            arms.append({"arm_key": key, "wins": 0.0, "losses": 0.0, "unsubs": 0, "disabled": 0})
    pending = {
        r["arm_key"]: int(r["n"])
        for r in query(
            "SELECT arm_key, COUNT(*) AS n FROM decisions WHERE workspace_id = ? AND flow = ? AND settled = 0 GROUP BY arm_key",
            (ws, flow),
        )
    }
    for a in arms:
        n = a["wins"] + a["losses"]
        a["n"] = n
        a["pending"] = pending.get(a["arm_key"], 0)
        a["rate"] = round(a["wins"] / n, 4) if n else None
    arms.sort(key=lambda a: (a["rate"] is None, -(a["rate"] or 0)))
    rates = {
        int(r["holdout"]): (r["avg_reward"], r["n"])
        for r in query(
            "SELECT holdout, AVG(reward) AS avg_reward, COUNT(*) AS n FROM decisions WHERE workspace_id = ? AND flow = ? AND settled = 1 GROUP BY holdout",
            (ws, flow),
        )
    }
    lift = None
    if 0 in rates and 1 in rates and rates[1][1] >= 30 and rates[1][0]:
        lift = round(float(rates[0][0]) / float(rates[1][0]) - 1, 3)
    return {"arms": arms, "learned_policy_vs_holdout_lift": lift,
            "holdout_n": rates.get(1, (0, 0))[1], "policy_n": rates.get(0, (0, 0))[1]}
 
 
# ----------------------------------------------------- reply intent (NB)
 
_TOKEN = re.compile(r"[a-z0-9\u0900-\u097f']+")
_SEED = {
    "positive": "interested yes haan ha price cost kitna details demo brochure catalog sure ok okay share call tell more",
    "negative": "no nahi not interested later busy wrong number remove dont don't later",
}
_SEED_N = 3
_LABELS = ("positive", "negative", "neutral")
 
 
def _tokens(text: str) -> list[str]:
    return [t[:60] for t in _TOKEN.findall((text or "").lower())][:60]
 
 
def classify(ws: str, text: str) -> tuple[str, float]:
    toks = _tokens(text)
    if not toks:
        return "neutral", 0.0
    marks = ",".join("?" for _ in toks)
    counts: dict[tuple[str, str], int] = {}
    for r in query(f"SELECT label, token, n FROM nb_tokens WHERE workspace_id = ? AND token IN ({marks})", (ws, *toks)):
        counts[(r["label"], r["token"])] = r["n"]
    totals = {r["label"]: r["t"] for r in query("SELECT label, SUM(n) AS t FROM nb_tokens WHERE workspace_id = ? GROUP BY label", (ws,))}
    vocab = 2000.0
    scores = {}
    for label in _LABELS:
        seed = set(_SEED.get(label, "").split())
        total = float(totals.get(label) or 0) + _SEED_N * len(seed)
        score = math.log(1 / len(_LABELS))
        for tok in toks:
            n = counts.get((label, tok), 0) + (_SEED_N if tok in seed else 0)
            score += math.log((n + 1) / (total + vocab))
        scores[label] = score
    top = max(scores.values())
    exp = {k: math.exp(v - top) for k, v in scores.items()}
    z = sum(exp.values())
    label = max(exp, key=exp.get)
    confidence = exp[label] / z
    return (label, confidence) if confidence >= 0.55 else ("neutral", confidence)
 
 
def train(ws: str, text: str, label: str) -> None:
    """Call with human-confirmed labels, e.g. when a lead moves to booked/won (positive) or lost (negative)."""
    if label not in _LABELS:
        return
    for tok in set(_tokens(text)):
        execute(
            "INSERT INTO nb_tokens (workspace_id, label, token, n) VALUES (?, ?, ?, 1) ON DUPLICATE KEY UPDATE n = n + 1",
            (ws, label, tok),
        )
 
 
def learn_nightly() -> dict:
    ensure_tables()
    return settle()