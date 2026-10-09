# Expense Claim Policy Review Assistant

An internal review workspace combining deterministic expense validation with policy-grounded AI assistance. It is designed for a beginner to run locally and for reviewers to audit decisions.

## Stack and architecture
- **React + CSS**: responsive workspace with dashboard, intake form, queue, details, history, and policy library.
- **FastAPI + SQLite**: portable API and persistent storage for users, claims, AI results, policy rules, and history.
- **Claude Sonnet 5.5 through `emergentintegrations`**: optional server-side analysis. Without a key or when unavailable, the app clearly shows **DEMO MODE** or **DEMO FALLBACK**, uses stored policy text, and routes uncertainty to a human.
- **JWT + salted password hashes**: simple registration/login for the reviewer workspace.

The API validates fields, dates, currencies, receipt status, duplicate signatures, category limits, and totals before AI. The AI never overrides deterministic findings.

## Run in VS Code on Windows
1. Install Python 3.11+ and Node.js 18+.
2. In PowerShell:
```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt
Copy-Item backend\.env.example backend\.env
```
3. Set a long `JWT_SECRET` in `backend/.env`. Leave the AI placeholder for explicit demo mode, or add `EMERGENT_LLM_KEY`.
4. Start the API: `cd backend; uvicorn server:app --reload --port 8001`
5. In another terminal: `cd frontend; yarn install; yarn start`
6. Open `http://localhost:3000`, register a reviewer, and submit a claim.

SQLite initializes automatically at first API start. Supported currencies are USD, EUR, GBP, CAD, and AUD.

## AI workflow and policy evidence
The app selects a stored policy category, sends the claim plus that policy and validation context to Claude, and stores the analysis. Every citation is copied from a stored rule. If the model fails, the fallback has low confidence and cannot automatically approve the claim. Configure `EMERGENT_LLM_KEY` only on the backend. The health endpoint reports the active mode (`LIVE AI` or `DEMO MODE`).

## Policy admin and import
Reviewers can edit the organizational policy from the **Policy admin** page without redeploying:
- Add, edit, and delete rules (code, title, text, category, claim limit).
- Export the current rule set as JSON, then re-import after edits.
- Import JSON or CSV. Columns required per row: `code,title,text,category,limit_amount`. Rules are **upserted by rule code** so existing codes update in place and new codes are created. Imports are transactional — a single invalid row aborts the whole apply, and `Preview` runs a dry-run without changes. Every change is written to the audit history (`policy:create`/`update`/`delete`/`import`).

## Tests
From the project root: `pytest backend/test_server.py -q`. The 13 offline tests cover required fields, future dates, positive amounts, duplicate detection, missing receipts, limits, AI fallback labelling, review actions, dashboard totals, persistent history, policy CRUD, and JSON/CSV import (apply and dry-run).

## Deployment
`render.yaml` is a beginner-friendly blueprint. Create a Web Service, set backend environment variables in the provider dashboard, set the frontend `REACT_APP_BACKEND_URL` to the backend URL before building, and use a persistent disk for `SQLITE_PATH` (for example `/data/expense_claims.db`). Verify `/api/health` and register a test reviewer.

**Deployment URL:** not yet available; deployment requires the user's hosting account login and manual approval. This is not a claim of a live deployment.

## Scope
Completed: claim intake, validation, duplicate detection, policy retrieval, AI/fallback analysis, citations, registration/login, reviewer decisions, overrides with reasons, dashboard totals, search, history, policy library, tests, docs, and deployment template.

Excluded: reimbursements, payment processing, payroll, tax advice, receipt OCR, and payment gateways. Review the sample policy before organizational use.

## Sample input
Try `Maya Chen`, `Meals`, `48.50 USD`, description `Team lunch with product team; purpose: quarterly planning`, with receipt checked. Try an unchecked receipt or amount over 75 USD to see human-review findings.