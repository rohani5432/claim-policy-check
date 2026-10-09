"""End-to-end live tests against the deployed preview URL.

Verifies LIVE AI mode, deterministic preservation, policy CRUD/import,
and audit entries for policy actions.
"""
import io
import os
import time
import uuid
from datetime import date

import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://claim-policy-check.preview.emergentagent.com").rstrip("/")


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


@pytest.fixture(scope="module")
def auth(session):
    email = f"live-{uuid.uuid4().hex[:8]}@example.com"
    r = session.post(f"{BASE_URL}/api/auth/register",
                     json={"name": "Live Tester", "email": email, "password": "password123"})
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    session.headers.update({"Authorization": f"Bearer {token}"})
    return {"email": email, "token": token}


def test_health_live_ai(session):
    r = session.get(f"{BASE_URL}/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["ai_mode"] == "LIVE AI", f"expected LIVE AI, got {body}"


def test_protected_routes_require_auth():
    r = requests.get(f"{BASE_URL}/api/claims")
    assert r.status_code == 401
    r = requests.get(f"{BASE_URL}/api/policy")
    assert r.status_code == 401


def test_live_ai_claim_submission_valid(session, auth):
    """A clean, within-limit claim: AI should be LIVE and should NOT fabricate citations."""
    body = {
        "claimant": "Live Tester",
        "claim_date": str(date.today()),
        "category": "Meals",
        "amount": 20,
        "currency": "USD",
        "description": "client lunch purpose: Q1 planning with partner",
        "receipt_available": True,
    }
    r = session.post(f"{BASE_URL}/api/claims", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    ai = data["ai"]
    assert ai["mode"] in ("LIVE AI", "DEMO FALLBACK", "DEMO MODE"), ai
    # If LIVE AI, the citation must be the stored MEALS-01 rule (no fabrication).
    if ai["mode"] == "LIVE AI":
        assert len(ai["citations"]) == 1
        assert ai["citations"][0]["code"] == "MEALS-01"
        assert 0.0 <= ai["confidence"] <= 1.0
        assert ai["decision"] in ("Pending Review", "Needs Human Review")


def test_live_ai_cannot_override_deterministic_review(session, auth):
    """A limit-exceeded claim must end up in Needs Human Review even when LIVE AI runs."""
    body = {
        "claimant": "Live Tester 2",
        "claim_date": str(date.today()),
        "category": "Meals",
        "amount": 500,  # exceeds MEALS-01 limit 75
        "currency": "USD",
        "description": "team dinner purpose with external vendors",
        "receipt_available": True,
    }
    r = session.post(f"{BASE_URL}/api/claims", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    codes = [f["code"] for f in data["validation"]["findings"]]
    assert "LIMIT-EXCEEDED" in codes
    assert data["status"] == "Needs Human Review"
    assert data["ai"]["decision"] == "Needs Human Review"
    assert data["ai"]["uncertain"] is True


def test_policy_crud_live(session, auth):
    code = f"LIVE-{uuid.uuid4().hex[:6].upper()}"
    created = session.post(f"{BASE_URL}/api/policy", json={
        "code": code, "title": "Live rule", "text": "Live imported policy rule.",
        "category": "Other", "limit_amount": 42,
    })
    assert created.status_code == 200, created.text

    # Duplicate -> 409
    dup = session.post(f"{BASE_URL}/api/policy", json={
        "code": code, "title": "Live rule", "text": "Live imported policy rule.",
        "category": "Other", "limit_amount": 42,
    })
    assert dup.status_code == 409

    updated = session.put(f"{BASE_URL}/api/policy/{code}", json={
        "title": "Live rule v2", "text": "Updated live rule text.",
        "category": "Other", "limit_amount": 55,
    })
    assert updated.status_code == 200
    assert updated.json()["limit_amount"] == 55

    deleted = session.delete(f"{BASE_URL}/api/policy/{code}")
    assert deleted.status_code == 200


def test_policy_import_json_and_audit(session, auth):
    code = f"IMP-{uuid.uuid4().hex[:6].upper()}"
    payload = (
        f'[{{"code":"{code}","title":"Imported","text":"Imported rule from upload.",'
        f'"category":"Other","limit_amount":120}}]'
    )
    files = {"file": ("rules.json", io.BytesIO(payload.encode()), "application/json")}
    data = {"apply": "true"}
    # Remove json content-type for multipart
    headers = {k: v for k, v in session.headers.items() if k.lower() != "content-type"}
    r = requests.post(f"{BASE_URL}/api/policy/import", headers=headers, files=files, data=data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["applied"] == 1 and body["errors"] == []

    # Audit entry for policy:import must exist with claim_id=null
    hist = session.get(f"{BASE_URL}/api/history").json()
    assert any(h["action"] == "policy:import" and h.get("claim_id") in (None,) for h in hist)
    assert any(h["action"] == "policy:create" for h in hist)
    assert any(h["action"] == "policy:update" for h in hist)
    assert any(h["action"] == "policy:delete" for h in hist)

    # cleanup
    session.delete(f"{BASE_URL}/api/policy/{code}")


def test_policy_import_csv_dry_run(session, auth):
    csv_text = (
        "code,title,text,category,limit_amount\n"
        "CSV-DRY,Imported CSV,This is a dry-run imported CSV rule,Other,42\n"
    )
    headers = {k: v for k, v in session.headers.items() if k.lower() != "content-type"}
    r = requests.post(
        f"{BASE_URL}/api/policy/import",
        headers=headers,
        files={"file": ("rules.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"apply": "false"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] == 1 and body["applied"] == 0 and body["dry_run"] is True


def test_policy_import_csv_errors_abort(session, auth):
    csv_text = (
        "code,title,text,category,limit_amount\n"
        "CSV-OK2,Imported CSV,This is a safe imported CSV rule,Other,42\n"
        "bad code,Broken,Short,Other,-5\n"
    )
    headers = {k: v for k, v in session.headers.items() if k.lower() != "content-type"}
    r = requests.post(
        f"{BASE_URL}/api/policy/import",
        headers=headers,
        files={"file": ("rules.csv", io.BytesIO(csv_text.encode()), "text/csv")},
        data={"apply": "true"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 0
    assert body["errors"] and body["valid"] == 1


def test_reviewer_action_requires_reason(session, auth):
    create = session.post(f"{BASE_URL}/api/claims", json={
        "claimant": "Action Tester",
        "claim_date": str(date.today()),
        "category": "Meals",
        "amount": 15,
        "currency": "USD",
        "description": "coffee purpose client meeting",
        "receipt_available": True,
    })
    assert create.status_code == 200
    cid = create.json()["id"]
    bad = session.post(f"{BASE_URL}/api/claims/{cid}/action", json={"action": "reject", "reason": ""})
    assert bad.status_code == 422
    ok = session.post(f"{BASE_URL}/api/claims/{cid}/action",
                      json={"action": "reject", "reason": "Out of policy per manager"})
    assert ok.status_code == 200
