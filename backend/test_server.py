import os,tempfile
from datetime import date,timedelta
os.environ["SQLITE_PATH"]=tempfile.mktemp(suffix=".db")
from fastapi.testclient import TestClient
from server import app,init_db,deterministic,ClaimIn
init_db(); client=TestClient(app)
def token():
 r=client.post("/api/auth/register",json={"name":"Test Reviewer","email":"test@example.com","password":"password123"})
 return r.json().get("token") or client.post("/api/auth/login",json={"email":"test@example.com","password":"password123"}).json()["token"]
def h(): return {"Authorization":f"Bearer {token()}"}
def test_required_and_amount_validation():
 r=client.post("/api/auth/register",json={"name":"A","email":"bad","password":"x"}); assert r.status_code==422
 try: ClaimIn(claimant="A",claim_date=date.today(),category="Meals",amount=-1,currency="USD",description="meal",receipt_available=True)
 except Exception: pass
 else: assert False
def test_future_date_validation():
 try: ClaimIn(claimant="A Person",claim_date=date.today()+timedelta(days=1),category="Meals",amount=4,currency="USD",description="meal",receipt_available=True)
 except Exception: pass
 else: assert False
def test_missing_receipt_and_limit():
 c=ClaimIn(claimant="A Person",claim_date=date.today(),category="Meals",amount=800,currency="USD",description="team meal purpose",receipt_available=False); codes=[x["code"] for x in deterministic(c)["findings"]]; assert "RECEIPT-MISSING" in codes and "LIMIT-EXCEEDED" in codes
 c=ClaimIn(claimant="A Person",claim_date=date.today(),category="Meals",amount=76,currency="USD",description="team meal purpose",receipt_available=True); assert "LIMIT-EXCEEDED" in [x["code"] for x in deterministic(c)["findings"]]
def test_claim_persists_history_and_demo_ai():
 r=client.post("/api/claims",headers=h(),json={"claimant":"A Person","claim_date":str(date.today()),"category":"Other","amount":25,"currency":"USD","description":"misc business item","receipt_available":True}); assert r.status_code==200; assert r.json()["ai"]["mode"] in ("DEMO MODE","DEMO FALLBACK")
 assert client.get("/api/history",headers=h()).json()
def test_reviewer_action_requires_reason():
 assert client.post("/api/claims/1/action",headers=h(),json={"action":"reject","reason":""}).status_code==422