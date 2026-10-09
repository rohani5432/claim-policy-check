"""Expense Claim Policy Review Assistant API.

- SQLite persistence for users, claims, policy rules, and audit history
- Deterministic validation runs first and is authoritative over AI
- AI classification/explanation via Claude Sonnet 5.5 (Emergent LLM Key); honest
  demo/fallback states when the key is missing or the provider fails
- Reviewer actions: approve / reject / clarification / override (audited)
- Policy rules are database-backed and editable; JSON/CSV import upserts by code
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import re
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, List, Optional

import jwt
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field, field_validator

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

DB_PATH = Path(os.getenv("SQLITE_PATH", str(ROOT / "expense_claims.db")))
JWT_SECRET = os.getenv("JWT_SECRET", "local-development-secret-change-me")
PASSWORD_SALT = os.getenv("PASSWORD_SALT", "expense-review-salt")
SUPPORTED_CURRENCIES = ("USD", "EUR", "GBP", "CAD", "AUD")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("expense-review")

security = HTTPBearer(auto_error=False)

# Seed policy. These rows are inserted only on first run; subsequent edits/imports
# in the database take precedence.
SEED_POLICY = [
    {"code": "TRAVEL-01", "title": "Travel and accommodation", "category": "Travel", "limit_amount": 2500,
     "text": "Flights, trains, lodging, and ground transport are eligible when tied to business travel. Lodging is capped at 250 USD per night; economy airfare is required unless a documented exception is approved."},
    {"code": "MEALS-01", "title": "Business meals", "category": "Meals", "limit_amount": 75,
     "text": "Business meals are eligible up to 75 USD per claim and must list the business purpose and attendees. Alcohol is not reimbursable."},
    {"code": "OFFICE-01", "title": "Office supplies", "category": "Office Supplies", "limit_amount": 300,
     "text": "Reasonable office supplies are eligible up to 300 USD per claim. Personal items and equipment purchases require pre-approval."},
    {"code": "TECH-01", "title": "Software and technology", "category": "Technology", "limit_amount": 1000,
     "text": "Software subscriptions and technology expenses require manager approval and are limited to 1000 USD per claim."},
    {"code": "OTHER-01", "title": "Other expenses", "category": "Other", "limit_amount": 500,
     "text": "Expenses outside listed categories require a clear business purpose and human review before approval."},
]

CATEGORY_ALIASES = {
    "food": "Meals", "dining": "Meals", "lunch": "Meals", "dinner": "Meals",
    "hotel": "Travel", "flight": "Travel", "train": "Travel", "taxi": "Travel",
    "software": "Technology", "laptop": "Technology", "saas": "Technology",
    "stationery": "Office Supplies", "supplies": "Office Supplies",
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'reviewer',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS claims(
                id INTEGER PRIMARY KEY, claimant TEXT NOT NULL, claim_date TEXT NOT NULL,
                category TEXT NOT NULL, amount REAL NOT NULL, currency TEXT NOT NULL,
                description TEXT NOT NULL, receipt_available INTEGER NOT NULL,
                status TEXT NOT NULL, validation_json TEXT NOT NULL, ai_json TEXT NOT NULL,
                created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(created_by) REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS history(
                id INTEGER PRIMARY KEY, claim_id INTEGER, action TEXT NOT NULL,
                reason TEXT, actor_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                FOREIGN KEY(actor_id) REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS policy(
                code TEXT PRIMARY KEY, title TEXT NOT NULL, text TEXT NOT NULL,
                category TEXT NOT NULL, limit_amount REAL NOT NULL,
                updated_at TEXT NOT NULL DEFAULT '');
            """
        )
        # Lightweight migration: older DBs may be missing updated_at.
        cols = {r[1] for r in c.execute("PRAGMA table_info(policy)").fetchall()}
        if "updated_at" not in cols:
            c.execute("ALTER TABLE policy ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
        # claim_id in history may be NOT NULL in older DBs — allow NULL for policy audits.
        hist_cols = c.execute("PRAGMA table_info(history)").fetchall()
        claim_id_col = next((r for r in hist_cols if r[1] == "claim_id"), None)
        if claim_id_col and claim_id_col[3] == 1:  # notnull flag
            c.executescript(
                "CREATE TABLE history_new(id INTEGER PRIMARY KEY, claim_id INTEGER, action TEXT NOT NULL, "
                "reason TEXT, actor_id INTEGER NOT NULL, created_at TEXT NOT NULL);"
                "INSERT INTO history_new SELECT id,claim_id,action,reason,actor_id,created_at FROM history;"
                "DROP TABLE history; ALTER TABLE history_new RENAME TO history;"
            )
        for p in SEED_POLICY:
            c.execute(
                "INSERT OR IGNORE INTO policy(code,title,text,category,limit_amount,updated_at) VALUES(?,?,?,?,?,?)",
                (p["code"], p["title"], p["text"], p["category"], p["limit_amount"], now()),
            )


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def password_hash(value: str) -> str:
    return hashlib.sha256((value + PASSWORD_SALT).encode()).hexdigest()


def token_for(user: dict) -> str:
    return jwt.encode(
        {"sub": str(user["id"]), "email": user["email"], "role": user["role"],
         "exp": datetime.now(timezone.utc) + timedelta(hours=12)},
        JWT_SECRET, algorithm="HS256",
    )


def auth_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    if not creds:
        raise HTTPException(401, "Please sign in to continue")
    try:
        payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=["HS256"])
        with db() as c:
            u = c.execute("SELECT id,name,email,role FROM users WHERE id=?", (payload["sub"],)).fetchone()
        if not u:
            raise ValueError("unknown user")
        return dict(u)
    except Exception:
        raise HTTPException(401, "Your session is invalid or expired")


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class RegisterIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    password: str = Field(min_length=8, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class ClaimIn(BaseModel):
    claimant: str = Field(min_length=2, max_length=120)
    claim_date: date
    category: str = Field(min_length=2, max_length=50)
    amount: float = Field(gt=0, le=1_000_000)
    currency: str = Field(pattern="^(USD|EUR|GBP|CAD|AUD)$")
    description: str = Field(min_length=5, max_length=1000)
    receipt_available: bool

    @field_validator("claim_date")
    @classmethod
    def not_future(cls, v: date) -> date:
        if v > date.today():
            raise ValueError("Claim date cannot be in the future")
        return v


class ActionIn(BaseModel):
    action: str = Field(pattern="^(approve|reject|clarification|override)$")
    reason: str = Field(default="", max_length=500)
    category: Optional[str] = None


class PolicyIn(BaseModel):
    code: str = Field(min_length=2, max_length=40, pattern=r"^[A-Z0-9][A-Z0-9\-_]*$")
    title: str = Field(min_length=2, max_length=120)
    text: str = Field(min_length=5, max_length=2000)
    category: str = Field(min_length=2, max_length=50)
    limit_amount: float = Field(gt=0, le=1_000_000)


class PolicyUpdateIn(BaseModel):
    title: str = Field(min_length=2, max_length=120)
    text: str = Field(min_length=5, max_length=2000)
    category: str = Field(min_length=2, max_length=50)
    limit_amount: float = Field(gt=0, le=1_000_000)


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------
def load_policy_rows() -> List[dict]:
    with db() as c:
        rows = c.execute("SELECT code,title,text,category,limit_amount FROM policy ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def resolve_category(category: str, description: str, rules: List[dict]) -> dict:
    """Pick the best policy rule for (category, description).

    Preference order: exact category match, alias from description, 'Other' fallback.
    """
    by_cat: dict[str, dict] = {}
    for r in rules:
        by_cat.setdefault(r["category"], r)

    chosen = CATEGORY_ALIASES.get(category.lower(), category)
    if chosen in by_cat:
        return by_cat[chosen]

    text = f"{category} {description}".lower()
    for alias, mapped in CATEGORY_ALIASES.items():
        if alias in text and mapped in by_cat:
            return by_cat[mapped]

    return by_cat.get("Other") or (rules[0] if rules else {
        "code": "NONE", "title": "No policy configured", "text": "", "category": "Other", "limit_amount": 0,
    })


# ---------------------------------------------------------------------------
# Deterministic validation
# ---------------------------------------------------------------------------
def deterministic(claim: ClaimIn) -> dict:
    rules = load_policy_rows()
    rule = resolve_category(claim.category, claim.description, rules)
    findings: List[dict] = []

    if not claim.receipt_available:
        findings.append({"code": "RECEIPT-MISSING", "severity": "review",
                         "message": "Receipt is missing; attach one or explain the exception."})

    if claim.amount > rule["limit_amount"]:
        findings.append({"code": "LIMIT-EXCEEDED", "severity": "review",
                         "message": f"Amount exceeds the {rule['category']} limit of {rule['limit_amount']:.2f} {claim.currency}."})

    with db() as c:
        dup = c.execute(
            "SELECT id FROM claims WHERE claimant=? AND claim_date=? AND amount=? AND currency=? AND lower(description)=lower(?)",
            (claim.claimant, claim.claim_date.isoformat(), claim.amount, claim.currency, claim.description),
        ).fetchone()
    if dup:
        findings.append({"code": "DUPLICATE-POSSIBLE", "severity": "review",
                         "message": f"Possible duplicate of claim #{dup['id']}."})

    if rule["category"] == "Meals" and "purpose" not in claim.description.lower():
        findings.append({"code": "MEALS-PURPOSE", "severity": "clarify",
                         "message": "Business purpose is needed for meals."})

    return {
        "valid": not any(f["severity"] == "review" for f in findings),
        "findings": findings,
        "total": claim.amount,
        "policy_limit": rule["limit_amount"],
        "policy_code": rule["code"],
    }


# ---------------------------------------------------------------------------
# AI workflow (Claude Sonnet 5.5 via Emergent LLM Key)
# ---------------------------------------------------------------------------
def _demo_result(rule: dict, validation: dict, mode: str, reason: str) -> dict:
    uncertain = rule["category"] == "Other" or any(f["severity"] == "clarify" for f in validation["findings"])
    needs_human = uncertain or any(f["severity"] == "review" for f in validation["findings"])
    return {
        "mode": mode,
        "classification": rule["category"],
        "confidence": 0.35 if "FALLBACK" in mode else (0.58 if uncertain else 0.86),
        "uncertain": needs_human,
        "decision": "Needs Human Review" if needs_human else "Pending Review",
        "explanation": reason,
        "citations": [{"code": rule["code"], "quote": rule["text"]}],
        "questions": [f["message"] for f in validation["findings"] if f["severity"] == "clarify"],
    }


async def ai_review(claim: ClaimIn, rule: dict, validation: dict) -> dict:
    """Return structured AI analysis. Never fabricate citations; cite the resolved rule only."""
    key = os.getenv("EMERGENT_LLM_KEY", "").strip()
    if not key or key.lower().startswith("your_"):
        logger.info("ai_review mode=DEMO reason=no_key claim_category=%s", rule["category"])
        return _demo_result(
            rule, validation, "DEMO MODE",
            "Demo analysis using stored policy text and deterministic checks only. Configure EMERGENT_LLM_KEY to enable live Claude Sonnet 5.5.",
        )

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except Exception as exc:  # library missing
        logger.warning("emergentintegrations import failed: %s", exc)
        return _demo_result(rule, validation, "DEMO FALLBACK",
                             "AI library unavailable on the server; falling back to deterministic analysis.")

    system = (
        "You are a strict expense policy reviewer. Analyse the claim ONLY against the supplied policy rule. "
        "Return a single JSON object with keys: classification (one of the policy categories), "
        "confidence (0..1), uncertain (bool), decision (one of 'Pending Review','Needs Human Review'), "
        "explanation (<=400 chars, plain prose), questions (array of strings for the claimant when info is missing). "
        "Never invent policy rules. Do not approve. Mark uncertain=true and decision='Needs Human Review' when unsure "
        "or when any deterministic finding has severity 'review'."
    )
    payload = {
        "claim": claim.model_dump(mode="json"),
        "policy_rule": rule,
        "deterministic_findings": validation["findings"],
    }

    try:
        chat = LlmChat(
            api_key=key,
            session_id=f"claim-{secrets.token_hex(6)}",
            system_message=system,
        ).with_model("anthropic", "claude-sonnet-5-5")
        response = await chat.send_message(UserMessage(text=json.dumps(payload)))
        raw = response if isinstance(response, str) else str(response)
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            raise ValueError("no JSON object in model response")
        parsed = json.loads(match.group(0))
    except Exception as exc:
        logger.warning("AI workflow failed type=%s msg=%s", type(exc).__name__, str(exc)[:200])
        return _demo_result(
            rule, validation, "DEMO FALLBACK",
            "The AI service was unavailable or returned an unparseable response. Deterministic findings still apply.",
        )

    # Defensive normalisation — never let the AI override deterministic review flags.
    classification = str(parsed.get("classification") or rule["category"])
    confidence = float(parsed.get("confidence") or 0.5)
    confidence = max(0.0, min(1.0, confidence))
    questions = [str(q) for q in (parsed.get("questions") or []) if str(q).strip()][:6]
    explanation = str(parsed.get("explanation") or "Live AI analysis completed.")[:800]

    has_review_flag = any(f["severity"] == "review" for f in validation["findings"])
    model_uncertain = bool(parsed.get("uncertain")) or confidence < 0.6
    decision = str(parsed.get("decision") or "").strip() or "Pending Review"
    if has_review_flag or model_uncertain:
        decision = "Needs Human Review"
        uncertain = True
    else:
        uncertain = False
        if decision not in ("Pending Review", "Needs Human Review"):
            decision = "Pending Review"

    return {
        "mode": "LIVE AI",
        "classification": classification,
        "confidence": confidence,
        "uncertain": uncertain,
        "decision": decision,
        "explanation": explanation,
        "citations": [{"code": rule["code"], "quote": rule["text"]}],
        "questions": questions,
    }


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
def claim_out(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["receipt_available"] = bool(d["receipt_available"])
    d["validation"] = json.loads(d.pop("validation_json"))
    d["ai"] = json.loads(d.pop("ai_json"))
    return d


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(title="Expense Claim Policy Review Assistant")
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.getenv("CORS_ORIGINS", "*").split(","),
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)
init_db()


@app.get("/api/health")
def health() -> dict:
    key_present = bool(os.getenv("EMERGENT_LLM_KEY", "").strip()) and not os.getenv("EMERGENT_LLM_KEY", "").lower().startswith("your_")
    return {"ok": True, "service": "expense-policy-review", "ai_mode": "LIVE AI" if key_present else "DEMO MODE"}


# -- auth --------------------------------------------------------------------
@app.post("/api/auth/register")
def register(data: RegisterIn) -> dict:
    with db() as c:
        try:
            cur = c.execute(
                "INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",
                (data.name, data.email.lower(), password_hash(data.password), now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "An account with this email already exists")
        u = {"id": cur.lastrowid, "name": data.name, "email": data.email.lower(), "role": "reviewer"}
    logger.info("user registered id=%s", u["id"])
    return {"token": token_for(u), "user": u}


@app.post("/api/auth/login")
def login(data: LoginIn) -> dict:
    with db() as c:
        u = c.execute(
            "SELECT id,name,email,role FROM users WHERE email=? AND password_hash=?",
            (data.email.lower(), password_hash(data.password)),
        ).fetchone()
    if not u:
        raise HTTPException(401, "Email or password is incorrect")
    return {"token": token_for(u), "user": dict(u)}


@app.get("/api/auth/me")
def me(user: dict = Depends(auth_user)) -> dict:
    return user


# -- claims ------------------------------------------------------------------
@app.post("/api/claims")
async def create_claim(data: ClaimIn, user: dict = Depends(auth_user)) -> dict:
    validation = deterministic(data)
    rule = resolve_category(data.category, data.description, load_policy_rows())
    ai = await ai_review(data, rule, validation)
    needs_human = (not validation["valid"]) or ai.get("uncertain") or ai["decision"] == "Needs Human Review"
    status = "Needs Human Review" if needs_human else "Pending Review"
    with db() as c:
        cur = c.execute(
            "INSERT INTO claims(claimant,claim_date,category,amount,currency,description,receipt_available,status,validation_json,ai_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (data.claimant, data.claim_date.isoformat(), rule["category"], data.amount, data.currency,
             data.description, int(data.receipt_available), status, json.dumps(validation), json.dumps(ai),
             user["id"], now()),
        )
        cid = cur.lastrowid
        c.execute(
            "INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
            (cid, "submitted", "", user["id"], now()),
        )
        row = c.execute("SELECT * FROM claims WHERE id=?", (cid,)).fetchone()
    logger.info("claim created id=%s status=%s ai_mode=%s", cid, status, ai.get("mode"))
    return claim_out(row)


@app.get("/api/claims")
def claims(status: Optional[str] = None, search: Optional[str] = None, user: dict = Depends(auth_user)) -> list:
    q = "SELECT * FROM claims WHERE 1=1"
    args: list[Any] = []
    if status:
        q += " AND status=?"; args.append(status)
    if search:
        q += " AND (lower(claimant) LIKE ? OR lower(description) LIKE ?)"
        args += [f"%{search.lower()}%"] * 2
    with db() as c:
        rows = c.execute(q + " ORDER BY created_at DESC", args).fetchall()
    return [claim_out(r) for r in rows]


@app.get("/api/claims/{claim_id}")
def get_claim(claim_id: int, user: dict = Depends(auth_user)) -> dict:
    with db() as c:
        row = c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Claim not found")
    return claim_out(row)


@app.post("/api/claims/{claim_id}/action")
def action(claim_id: int, data: ActionIn, user: dict = Depends(auth_user)) -> dict:
    if data.action in ("reject", "override") and len(data.reason.strip()) < 5:
        raise HTTPException(422, "A reason is required for this action")
    labels = {"approve": "Approved", "reject": "Rejected",
              "clarification": "Clarification Requested", "override": "Needs Human Review"}
    with db() as c:
        row = c.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Claim not found")
        category = data.category if data.action == "override" and data.category else row["category"]
        status = labels[data.action]
        c.execute("UPDATE claims SET status=?,category=? WHERE id=?", (status, category, claim_id))
        c.execute(
            "INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
            (claim_id, data.action, data.reason, user["id"], now()),
        )
    logger.info("claim action id=%s action=%s actor=%s", claim_id, data.action, user["id"])
    return {"status": status, "claim_id": claim_id}


@app.get("/api/history")
def history(user: dict = Depends(auth_user)) -> list:
    with db() as c:
        rows = c.execute(
            "SELECT h.*,u.name actor FROM history h JOIN users u ON u.id=h.actor_id ORDER BY h.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/dashboard")
def dashboard(user: dict = Depends(auth_user)) -> dict:
    with db() as c:
        total = c.execute("SELECT COUNT(*) count,COALESCE(SUM(amount),0) total FROM claims").fetchone()
        statuses = c.execute("SELECT status,COUNT(*) count FROM claims GROUP BY status").fetchall()
    return {"count": total["count"], "total": total["total"], "statuses": {r["status"]: r["count"] for r in statuses}}


# -- policy ------------------------------------------------------------------
@app.get("/api/policy")
def policy_list(user: dict = Depends(auth_user)) -> list:
    with db() as c:
        rows = c.execute("SELECT * FROM policy ORDER BY code").fetchall()
    return [dict(r) for r in rows]


def _policy_audit(actor_id: int, code: str, action: str) -> None:
    with db() as c:
        c.execute(
            "INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
            (None, f"policy:{action}", code, actor_id, now()),
        )


@app.post("/api/policy")
def policy_create(data: PolicyIn, user: dict = Depends(auth_user)) -> dict:
    with db() as c:
        try:
            c.execute(
                "INSERT INTO policy(code,title,text,category,limit_amount,updated_at) VALUES(?,?,?,?,?,?)",
                (data.code, data.title, data.text, data.category, data.limit_amount, now()),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "A rule with this code already exists")
    _policy_audit(user["id"], data.code, "create")
    logger.info("policy created code=%s by=%s", data.code, user["id"])
    return data.model_dump()


@app.put("/api/policy/{code}")
def policy_update(code: str, data: PolicyUpdateIn, user: dict = Depends(auth_user)) -> dict:
    with db() as c:
        row = c.execute("SELECT code FROM policy WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, "Policy rule not found")
        c.execute(
            "UPDATE policy SET title=?,text=?,category=?,limit_amount=?,updated_at=? WHERE code=?",
            (data.title, data.text, data.category, data.limit_amount, now(), code),
        )
    _policy_audit(user["id"], code, "update")
    logger.info("policy updated code=%s by=%s", code, user["id"])
    return {"code": code, **data.model_dump()}


@app.delete("/api/policy/{code}")
def policy_delete(code: str, user: dict = Depends(auth_user)) -> dict:
    with db() as c:
        cur = c.execute("DELETE FROM policy WHERE code=?", (code,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Policy rule not found")
    _policy_audit(user["id"], code, "delete")
    logger.info("policy deleted code=%s by=%s", code, user["id"])
    return {"deleted": code}


def _parse_import_rows(content: bytes, filename: str) -> List[dict]:
    name = (filename or "").lower()
    text = content.decode("utf-8-sig", errors="replace")
    rows: List[dict] = []
    if name.endswith(".json") or text.lstrip().startswith(("[", "{")):
        data = json.loads(text)
        if isinstance(data, dict) and "rules" in data:
            data = data["rules"]
        if not isinstance(data, list):
            raise ValueError("JSON must be an array of rules or an object with a 'rules' array")
        rows = [dict(r) for r in data]
    else:
        reader = csv.DictReader(io.StringIO(text))
        rows = [dict(r) for r in reader]
    return rows


@app.post("/api/policy/import")
async def policy_import(file: UploadFile = File(...), apply: str = Form("true"), user: dict = Depends(auth_user)) -> dict:
    """Upsert policy rules by code from a JSON or CSV upload.

    Required fields per row: code, title, text, category, limit_amount.
    - apply=false -> dry run / preview only.
    - apply=true  -> transactional upsert; any invalid row aborts the whole import.
    """
    try:
        raw = await file.read()
        rows = _parse_import_rows(raw, file.filename or "")
    except Exception as exc:
        raise HTTPException(400, f"Could not parse file: {exc}")

    valid: List[dict] = []
    errors: List[dict] = []
    for i, raw_row in enumerate(rows):
        try:
            row = {k.strip(): v for k, v in raw_row.items() if k}
            row["limit_amount"] = float(row.get("limit_amount") or 0)
            parsed = PolicyIn(**row)
            valid.append(parsed.model_dump())
        except Exception as exc:
            errors.append({"row": i + 1, "code": raw_row.get("code"), "error": str(exc)})

    do_apply = str(apply).lower() in ("1", "true", "yes", "on")
    applied = 0
    if do_apply and not errors and valid:
        with db() as c:
            for r in valid:
                c.execute(
                    "INSERT INTO policy(code,title,text,category,limit_amount,updated_at) VALUES(?,?,?,?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET title=excluded.title,text=excluded.text,"
                    "category=excluded.category,limit_amount=excluded.limit_amount,updated_at=excluded.updated_at",
                    (r["code"], r["title"], r["text"], r["category"], r["limit_amount"], now()),
                )
            c.execute(
                "INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",
                (None, "policy:import", f"{len(valid)} rules upserted", user["id"], now()),
            )
            applied = len(valid)
        logger.info("policy import applied=%s by=%s", applied, user["id"])

    return {
        "parsed": len(rows),
        "valid": len(valid),
        "errors": errors,
        "applied": applied,
        "dry_run": not do_apply or bool(errors),
        "preview": valid[:50],
    }
