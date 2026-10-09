"""Expense Claim Policy Review Assistant API."""
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import hashlib, json, logging, os, re, secrets, sqlite3
from typing import Optional
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, EmailStr, field_validator
import jwt

ROOT=Path(__file__).parent; load_dotenv(ROOT/".env")
DB_PATH=Path(os.getenv("SQLITE_PATH",str(ROOT/"expense_claims.db")))
JWT_SECRET=os.getenv("JWT_SECRET","local-development-secret-change-me"); security=HTTPBearer(auto_error=False)
logging.basicConfig(level=logging.INFO,format="%(asctime)s %(levelname)s %(message)s"); logger=logging.getLogger("expense-review")
POLICY=[
 {"code":"TRAVEL-01","title":"Travel and accommodation","text":"Flights, trains, lodging, and ground transport are eligible when tied to business travel. Lodging is capped at 250 USD per night; economy airfare is required unless a documented exception is approved.","category":"Travel","limit":2500},
 {"code":"MEALS-01","title":"Business meals","text":"Business meals are eligible up to 75 USD per person and must list the business purpose and attendees. Alcohol is not reimbursable.","category":"Meals","limit":600},
 {"code":"OFFICE-01","title":"Office supplies","text":"Reasonable office supplies are eligible up to 300 USD per claim. Personal items and equipment purchases require pre-approval.","category":"Office Supplies","limit":300},
 {"code":"TECH-01","title":"Software and technology","text":"Software subscriptions and technology expenses require manager approval and are limited to 1000 USD per claim.","category":"Technology","limit":1000},
 {"code":"OTHER-01","title":"Other expenses","text":"Expenses outside listed categories require a clear business purpose and human review before approval.","category":"Other","limit":500},]
def db():
 c=sqlite3.connect(DB_PATH); c.row_factory=sqlite3.Row; c.execute("PRAGMA foreign_keys=ON"); return c
def init_db():
 with db() as c:
  c.executescript("""CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY,name TEXT NOT NULL,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'reviewer',created_at TEXT NOT NULL); CREATE TABLE IF NOT EXISTS claims(id INTEGER PRIMARY KEY,claimant TEXT NOT NULL,claim_date TEXT NOT NULL,category TEXT NOT NULL,amount REAL NOT NULL,currency TEXT NOT NULL,description TEXT NOT NULL,receipt_available INTEGER NOT NULL,status TEXT NOT NULL,validation_json TEXT NOT NULL,ai_json TEXT NOT NULL,created_by INTEGER NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(created_by) REFERENCES users(id)); CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY,claim_id INTEGER NOT NULL,action TEXT NOT NULL,reason TEXT,actor_id INTEGER NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(claim_id) REFERENCES claims(id),FOREIGN KEY(actor_id) REFERENCES users(id)); CREATE TABLE IF NOT EXISTS policy(code TEXT PRIMARY KEY,title TEXT NOT NULL,text TEXT NOT NULL,category TEXT NOT NULL,limit_amount REAL NOT NULL);""")
  for p in POLICY: c.execute("INSERT OR IGNORE INTO policy VALUES(?,?,?,?,?)",(p["code"],p["title"],p["text"],p["category"],p["limit"]))
def now(): return datetime.now(timezone.utc).isoformat()
def password_hash(v): return hashlib.sha256((v+os.getenv("PASSWORD_SALT","expense-review-salt")).encode()).hexdigest()
def token_for(u): return jwt.encode({"sub":str(u["id"]),"email":u["email"],"role":u["role"],"exp":datetime.now(timezone.utc)+timedelta(hours=12)},JWT_SECRET,algorithm="HS256")
def auth_user(creds:HTTPAuthorizationCredentials=Depends(security)):
 if not creds: raise HTTPException(401,"Please sign in to continue")
 try:
  p=jwt.decode(creds.credentials,JWT_SECRET,algorithms=["HS256"])
  with db() as c: u=c.execute("SELECT id,name,email,role FROM users WHERE id=?",(p["sub"],)).fetchone()
  if not u: raise ValueError()
  return dict(u)
 except Exception: raise HTTPException(401,"Your session is invalid or expired")
class RegisterIn(BaseModel): name:str=Field(min_length=2,max_length=80); email:EmailStr; password:str=Field(min_length=8,max_length=120)
class LoginIn(BaseModel): email:EmailStr; password:str
class ClaimIn(BaseModel):
 claimant:str=Field(min_length=2,max_length=120); claim_date:date; category:str=Field(min_length=2,max_length=50); amount:float=Field(gt=0,le=1000000); currency:str=Field(pattern="^(USD|EUR|GBP|CAD|AUD)$"); description:str=Field(min_length=5,max_length=1000); receipt_available:bool
 @field_validator("claim_date")
 @classmethod
 def not_future(cls,v):
  if v>date.today(): raise ValueError("Claim date cannot be in the future")
  return v
class ActionIn(BaseModel): action:str=Field(pattern="^(approve|reject|clarification|override)$"); reason:str=Field(default="",max_length=500); category:Optional[str]=None
def policy_for(category,description):
 aliases={"food":"Meals","dining":"Meals","hotel":"Travel","flight":"Travel","train":"Travel","software":"Technology","laptop":"Technology","stationery":"Office Supplies"}; choices={p["category"]:p for p in POLICY}; text=(category+" "+description).lower(); chosen=aliases.get(category.lower(),category)
 if chosen not in choices:
  for key,value in aliases.items():
   if key in text: chosen=value; break
 return choices.get(chosen,choices["Other"])
def deterministic(claim):
 p=policy_for(claim.category,claim.description); findings=[]
 if not claim.receipt_available: findings.append({"code":"RECEIPT-MISSING","severity":"review","message":"Receipt is missing; attach one or explain the exception."})
 if claim.amount>p["limit"]: findings.append({"code":"LIMIT-EXCEEDED","severity":"review","message":f"Amount exceeds the {p['category']} limit of {p['limit']:.2f} {claim.currency}."})
 with db() as c: dup=c.execute("SELECT id FROM claims WHERE claimant=? AND claim_date=? AND amount=? AND currency=? AND lower(description)=lower(?)",(claim.claimant,claim.claim_date.isoformat(),claim.amount,claim.currency,claim.description)).fetchone()
 if dup: findings.append({"code":"DUPLICATE-POSSIBLE","severity":"review","message":f"Possible duplicate of claim #{dup['id']}."})
 if p["category"]=="Meals" and "purpose" not in claim.description.lower(): findings.append({"code":"MEALS-PURPOSE","severity":"clarify","message":"Business purpose is needed for meals."})
 return {"valid":not any(x["severity"]=="review" for x in findings),"findings":findings,"total":claim.amount,"policy_limit":p["limit"],"policy_code":p["code"]}
async def ai_review(claim,p,validation):
 key=os.getenv("EMERGENT_LLM_KEY","").strip()
 if not key or key.lower().startswith("your_"):
  uncertain=p["category"]=="Other"; return {"mode":"DEMO MODE","classification":p["category"],"confidence":0.58 if uncertain else 0.86,"uncertain":uncertain,"decision":"Needs Human Review" if uncertain or validation["findings"] else "Pending Review","explanation":"Demo analysis uses stored policy text and deterministic checks. Configure EMERGENT_LLM_KEY to enable Claude Sonnet 5.5.","citations":[{"code":p["code"],"quote":p["text"]}],"questions":[x["message"] for x in validation["findings"] if x["severity"]=="clarify"]}
 try:
  from emergentintegrations.llm.chat import LlmChat,UserMessage,TextDelta
  chat=LlmChat(api_key=key,session_id=f"claim-{secrets.token_hex(6)}",system_message="Review only against supplied policy. Return JSON with classification, confidence, decision, explanation, questions. Never invent citations.").with_model("anthropic","claude-sonnet-5-5"); out=""
  async for event in chat.stream_message(UserMessage(text=json.dumps({"claim":claim.model_dump(mode="json"),"policy":p,"validation":validation}))):
   if isinstance(event,TextDelta): out+=event.content
  parsed=json.loads(re.search(r"\{.*\}",out,re.S).group(0)); parsed.update({"mode":"LIVE AI","citations":[{"code":p["code"],"quote":p["text"]}]}); return parsed
 except Exception as exc:
  logger.warning("AI workflow failed: %s",type(exc).__name__); return {"mode":"DEMO FALLBACK","classification":p["category"],"confidence":0.35,"uncertain":True,"decision":"Needs Human Review","explanation":"The AI service was unavailable. No automatic approval was made.","citations":[{"code":p["code"],"quote":p["text"]}],"questions":["Please have a reviewer confirm the category and business purpose."]}
def claim_out(row):
 d=dict(row); d["receipt_available"]=bool(d["receipt_available"]); d["validation"]=json.loads(d.pop("validation_json")); d["ai"]=json.loads(d.pop("ai_json")); return d
app=FastAPI(title="Expense Claim Policy Review Assistant"); app.add_middleware(CORSMiddleware,allow_origins=os.getenv("CORS_ORIGINS","*").split(","),allow_credentials=True,allow_methods=["*"],allow_headers=["*"]); init_db()
@app.get("/api/health")
def health(): return {"ok":True,"service":"expense-policy-review"}
@app.post("/api/auth/register")
def register(data:RegisterIn):
 with db() as c:
  try: cur=c.execute("INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)",(data.name,data.email.lower(),password_hash(data.password),now()))
  except sqlite3.IntegrityError: raise HTTPException(409,"An account with this email already exists")
  u={"id":cur.lastrowid,"name":data.name,"email":data.email.lower(),"role":"reviewer"}
 return {"token":token_for(u),"user":u}
@app.post("/api/auth/login")
def login(data:LoginIn):
 with db() as c: u=c.execute("SELECT id,name,email,role FROM users WHERE email=? AND password_hash=?",(data.email.lower(),password_hash(data.password))).fetchone()
 if not u: raise HTTPException(401,"Email or password is incorrect")
 return {"token":token_for(u),"user":dict(u)}
@app.get("/api/auth/me")
def me(user=Depends(auth_user)): return user
@app.post("/api/claims")
async def create_claim(data:ClaimIn,user=Depends(auth_user)):
 validation=deterministic(data); p=policy_for(data.category,data.description); ai=await ai_review(data,p,validation); status="Needs Human Review" if (not validation["valid"] or ai.get("uncertain") or ai["decision"]=="Needs Human Review") else "Pending Review"
 with db() as c:
  cur=c.execute("INSERT INTO claims(claimant,claim_date,category,amount,currency,description,receipt_available,status,validation_json,ai_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(data.claimant,data.claim_date.isoformat(),p["category"],data.amount,data.currency,data.description,int(data.receipt_available),status,json.dumps(validation),json.dumps(ai),user["id"],now())); cid=cur.lastrowid; c.execute("INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",(cid,"submitted","",user["id"],now())); row=c.execute("SELECT * FROM claims WHERE id=?",(cid,)).fetchone()
 return claim_out(row)
@app.get("/api/claims")
def claims(status:Optional[str]=None,search:Optional[str]=None,user=Depends(auth_user)):
 q="SELECT * FROM claims WHERE 1=1"; args=[]
 if status: q+=" AND status=?"; args.append(status)
 if search: q+=" AND (lower(claimant) LIKE ? OR lower(description) LIKE ?)"; args += [f"%{search.lower()}%"]*2
 with db() as c: rows=c.execute(q+" ORDER BY created_at DESC",args).fetchall()
 return [claim_out(r) for r in rows]
@app.get("/api/claims/{claim_id}")
def get_claim(claim_id:int,user=Depends(auth_user)):
 with db() as c: row=c.execute("SELECT * FROM claims WHERE id=?",(claim_id,)).fetchone()
 if not row: raise HTTPException(404,"Claim not found")
 return claim_out(row)
@app.post("/api/claims/{claim_id}/action")
def action(claim_id:int,data:ActionIn,user=Depends(auth_user)):
 if data.action in ("reject","override") and len(data.reason.strip())<5: raise HTTPException(422,"A reason is required for this action")
 labels={"approve":"Approved","reject":"Rejected","clarification":"Clarification Requested","override":"Needs Human Review"}
 with db() as c:
  row=c.execute("SELECT * FROM claims WHERE id=?",(claim_id,)).fetchone()
  if not row: raise HTTPException(404,"Claim not found")
  category=data.category if data.action=="override" and data.category else row["category"]; status=labels[data.action]; c.execute("UPDATE claims SET status=?,category=? WHERE id=?",(status,category,claim_id)); c.execute("INSERT INTO history(claim_id,action,reason,actor_id,created_at) VALUES(?,?,?,?,?)",(claim_id,data.action,data.reason,user["id"],now()))
 return {"status":status,"claim_id":claim_id}
@app.get("/api/history")
def history(user=Depends(auth_user)):
 with db() as c: rows=c.execute("SELECT h.*,u.name actor FROM history h JOIN users u ON u.id=h.actor_id ORDER BY h.created_at DESC").fetchall()
 return [dict(r) for r in rows]
@app.get("/api/dashboard")
def dashboard(user=Depends(auth_user)):
 with db() as c: total=c.execute("SELECT COUNT(*) count,COALESCE(SUM(amount),0) total FROM claims").fetchone(); statuses=c.execute("SELECT status,COUNT(*) count FROM claims GROUP BY status").fetchall()
 return {"count":total["count"],"total":total["total"],"statuses":{r["status"]:r["count"] for r in statuses}}
@app.get("/api/policy")
def policy(user=Depends(auth_user)):
 with db() as c: rows=c.execute("SELECT * FROM policy ORDER BY code").fetchall()
 return [dict(r) for r in rows]