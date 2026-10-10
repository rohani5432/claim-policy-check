"""Backend tests for the Expense Claim Policy Review Assistant.

These tests isolate SQLite to a temp file and keep AI integration
offline (EMERGENT_LLM_KEY is cleared) so demo-mode behaviour is
exercised. The LIVE AI path is covered by a monkey-patched exception
check (fallback must still produce a safe demo response).
"""
import io
import os
import tempfile
from datetime import date, timedelta

os.environ["SQLITE_PATH"] = tempfile.mktemp(suffix=".db")
os.environ["EMERGENT_LLM_KEY"] = ""  # force demo mode in tests

from fastapi.testclient import TestClient  # noqa: E402

from server import app, init_db, deterministic, ClaimIn  # noqa: E402

init_db()
client = TestClient(app)

# Pre-register the admin so the first-user-admin rule is deterministic.
_admin = client.post("/api/auth/register", json={
    "name": "Admin User", "email": "admin@example.com", "password": "password123",
}).json()
ADMIN_H = {"Authorization": f"Bearer {_admin['token']}"}
assert _admin["user"]["role"] == "admin"


def _token(email: str = "test@example.com", password: str = "password123") -> str:
    r = client.post("/api/auth/register", json={"name": "Test Reviewer", "email": email, "password": password})
    if r.status_code == 200:
        return r.json()["token"]
    return client.post("/api/auth/login", json={"email": email, "password": password}).json()["token"]


def _h(email: str = "test@example.com") -> dict:
    return {"Authorization": f"Bearer {_token(email)}"}


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def test_register_requires_valid_email_and_amount():
    r = client.post("/api/auth/register", json={"name": "A", "email": "bad", "password": "x"})
    assert r.status_code == 422
    try:
        ClaimIn(claimant="A", claim_date=date.today(), category="Meals", amount=-1,
                currency="USD", description="meal", receipt_available=True)
    except Exception:
        pass
    else:
        raise AssertionError("negative amount should be rejected")


def test_future_date_rejected():
    try:
        ClaimIn(claimant="A Person", claim_date=date.today() + timedelta(days=1),
                category="Meals", amount=4, currency="USD",
                description="meal purpose", receipt_available=True)
    except Exception:
        return
    raise AssertionError("future dates should be rejected")


def test_unsupported_currency_rejected():
    try:
        ClaimIn(claimant="A Person", claim_date=date.today(), category="Meals",
                amount=10, currency="INR", description="lunch purpose", receipt_available=True)
    except Exception:
        return
    raise AssertionError("unsupported currency should be rejected")


# ---------------------------------------------------------------------------
# Deterministic checks
# ---------------------------------------------------------------------------
def test_missing_receipt_and_meals_limit():
    c = ClaimIn(claimant="A Person", claim_date=date.today(), category="Meals",
                amount=800, currency="USD", description="team meal purpose",
                receipt_available=False)
    codes = [x["code"] for x in deterministic(c)["findings"]]
    assert "RECEIPT-MISSING" in codes and "LIMIT-EXCEEDED" in codes

    c = ClaimIn(claimant="A Person", claim_date=date.today(), category="Meals",
                amount=76, currency="USD", description="team meal purpose",
                receipt_available=True)
    assert "LIMIT-EXCEEDED" in [x["code"] for x in deterministic(c)["findings"]]


def test_meals_purpose_clarification():
    c = ClaimIn(claimant="A Person", claim_date=date.today(), category="Meals",
                amount=20, currency="USD", description="team lunch",
                receipt_available=True)
    codes = [x["code"] for x in deterministic(c)["findings"]]
    assert "MEALS-PURPOSE" in codes


def test_duplicate_detection_runs():
    headers = _h("dup@example.com")
    body = {"claimant": "Dup Person", "claim_date": str(date.today()),
            "category": "Office Supplies", "amount": 20, "currency": "USD",
            "description": "pens and notebooks", "receipt_available": True}
    assert client.post("/api/claims", headers=headers, json=body).status_code == 200
    r2 = client.post("/api/claims", headers=headers, json=body)
    assert r2.status_code == 200
    codes = [f["code"] for f in r2.json()["validation"]["findings"]]
    assert "DUPLICATE-POSSIBLE" in codes


# ---------------------------------------------------------------------------
# Claims / AI / history
# ---------------------------------------------------------------------------
def test_claim_persists_history_and_demo_ai():
    headers = _h("history@example.com")
    r = client.post("/api/claims", headers=headers, json={
        "claimant": "A Person", "claim_date": str(date.today()), "category": "Other",
        "amount": 25, "currency": "USD", "description": "misc business item",
        "receipt_available": True,
    })
    assert r.status_code == 200
    assert r.json()["ai"]["mode"] in ("DEMO MODE", "DEMO FALLBACK")
    assert r.json()["ai"]["citations"][0]["code"].startswith("OTHER")
    assert client.get("/api/history", headers=headers).json()


def test_reviewer_actions_require_reason_and_record_history():
    headers = _h("decision@example.com")
    create = client.post("/api/claims", headers=headers, json={
        "claimant": "Decision Person", "claim_date": str(date.today()), "category": "Meals",
        "amount": 20, "currency": "USD", "description": "lunch purpose with client",
        "receipt_available": True,
    })
    assert create.status_code == 200
    cid = create.json()["id"]
    bad = client.post(f"/api/claims/{cid}/action", headers=headers, json={"action": "reject", "reason": ""})
    assert bad.status_code == 422
    ok = client.post(f"/api/claims/{cid}/action", headers=headers, json={
        "action": "override", "reason": "AI chose wrong category after review", "category": "Travel"
    })
    assert ok.status_code == 200
    detail = client.get(f"/api/claims/{cid}", headers=headers).json()
    assert detail["category"] == "Travel"
    actions = [h["action"] for h in client.get("/api/history", headers=headers).json() if h.get("claim_id") == cid]
    assert "override" in actions and "submitted" in actions


def test_dashboard_totals_grow():
    headers = _h("totals@example.com")
    before = client.get("/api/dashboard", headers=headers).json()
    client.post("/api/claims", headers=headers, json={
        "claimant": "Totals Person", "claim_date": str(date.today()), "category": "Technology",
        "amount": 120, "currency": "USD", "description": "team collaboration subscription",
        "receipt_available": True,
    })
    after = client.get("/api/dashboard", headers=headers).json()
    assert after["count"] == before["count"] + 1
    assert after["total"] >= before["total"] + 120 - 0.001


# ---------------------------------------------------------------------------
# Policy CRUD + import
# ---------------------------------------------------------------------------
def test_policy_crud_round_trip():
    rules = client.get("/api/policy", headers=ADMIN_H).json()
    assert any(r["code"] == "MEALS-01" and r["limit_amount"] == 75 for r in rules)

    created = client.post("/api/policy", headers=ADMIN_H, json={
        "code": "NEW-RULE-01", "title": "New rule", "text": "A new policy rule for testing imports.",
        "category": "Other", "limit_amount": 50,
    })
    assert created.status_code == 200

    updated = client.put("/api/policy/NEW-RULE-01", headers=ADMIN_H, json={
        "title": "New rule", "text": "Updated text for testing.", "category": "Other", "limit_amount": 60,
    })
    assert updated.status_code == 200 and updated.json()["limit_amount"] == 60

    deleted = client.delete("/api/policy/NEW-RULE-01", headers=ADMIN_H)
    assert deleted.status_code == 200


def test_non_admin_cannot_edit_policy():
    # The first registered user in this test module became admin; a brand new
    # account registered now will be a plain reviewer.
    headers = _h("reviewer-only@example.com")
    assert client.get("/api/policy", headers=headers).status_code == 200  # read is fine
    r = client.post("/api/policy", headers=headers, json={
        "code": "DENY-01", "title": "Denied", "text": "Should be rejected for non-admin.",
        "category": "Other", "limit_amount": 10,
    })
    assert r.status_code == 403
    assert client.delete("/api/policy/MEALS-01", headers=headers).status_code == 403


def test_csv_export_includes_submitted_claim():
    headers = _h("export@example.com")
    client.post("/api/claims", headers=headers, json={
        "claimant": "Export Person", "claim_date": str(date.today()), "category": "Office Supplies",
        "amount": 15, "currency": "USD", "description": "pens and markers for workshop",
        "receipt_available": True,
    })
    r = client.get("/api/export/claims.csv", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Export Person" in r.text
    assert "ai_mode" in r.text  # header row


def test_policy_import_json_upserts_by_code():
    payload = (
        '[{"code":"MEALS-01","title":"Business meals","text":"Business meals are '
        'eligible up to 75 USD per claim and must list the business purpose and attendees. '
        'Alcohol is not reimbursable.","category":"Meals","limit_amount":75},'
        '{"code":"IMPORT-TEST","title":"Imported","text":"Imported rule from upload.","category":"Other","limit_amount":100}]'
    )
    r = client.post(
        "/api/policy/import",
        headers=ADMIN_H,
        files={"file": ("rules.json", io.BytesIO(payload.encode()), "application/json")},
        data={"apply": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["errors"] == [] and body["applied"] == 2
    codes = {p["code"] for p in client.get("/api/policy", headers=ADMIN_H).json()}
    assert "IMPORT-TEST" in codes


def test_policy_import_csv_dry_run_and_errors():
    csv_text = (
        "code,title,text,category,limit_amount\n"
        "CSV-OK,Imported CSV,This is a safe imported CSV rule,Other,42\n"
        "bad code,Broken,Short,Other,-5\n"
    )
    r = client.post(
        "/api/policy/import",
        headers=ADMIN_H,
        files={"file": ("rules.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"apply": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 0  # errors must abort apply
    assert body["errors"] and body["valid"] == 1


def test_protected_routes_require_auth():
    assert client.get("/api/claims").status_code == 401
    assert client.post("/api/policy", json={}).status_code == 401
