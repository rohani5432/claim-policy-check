# Expense Claim Policy Review Assistant — PRD

## Original problem statement
Build a complete beginner-friendly expense claim review application that classifies claims against an organizational policy using deterministic validation first and AI (Claude Sonnet 5.5) second. See initial problem statement for full field list, scope, and exclusions.

## Architecture
- Backend: FastAPI + SQLite (users, claims, policy, history). JWT reviewer auth.
- Frontend: React (CRA + craco). Pages: Overview, Submit, Claims, Detail, History, Policy admin.
- AI: `anthropic/claude-sonnet-5-5` via `emergentintegrations.llm.chat.LlmChat` (non-streaming structured JSON). `EMERGENT_LLM_KEY` server-only. Honest `DEMO MODE` / `DEMO FALLBACK` labels on missing key or provider error.
- Deterministic validator is authoritative — the server coerces decision to `Needs Human Review` whenever a finding has severity `review`.

## Completed
- Reviewer registration / login, protected endpoints.
- Deterministic validation: required fields, future-date reject, unsupported currency reject, positive amount, duplicates, receipts, category limit, totals, meals-purpose clarify.
- Claim submit + AI analysis + persistent audit history.
- Reviewer actions: approve, reject (reason ≥ 5 chars), clarification, override (reason + optional category).
- Dashboard stats, All claims with status filter + search, claim detail with findings/citations.
- **LIVE AI** path with Claude Sonnet 5.5 and safe fallback (added this iteration).
- **Editable policy rules & category limits** (CRUD) with audit trail (added this iteration).
- **JSON / CSV policy import** — upsert by rule code, transactional, preview/apply (added this iteration).
- Tests: 13 offline pytest + 9 live E2E = 22 passing.
- Deliverable ZIP at `/app/Expense-Claim-Policy-Review-Assistant.zip` (no secrets).

## Known limitations
- No role separation (any reviewer can edit policy). No pagination/sorting on the claims table.
- Rate limiting is not enforced; live AI calls consume Emergent key balance per submission.
- No hosted deployment URL — deployment was intentionally skipped by the user.
- SQLite is single-node; multi-replica deployment needs an external DB.

## Backlog (P1 / P2)
- Role split (admin vs reviewer) for policy edits/imports.
- Pagination + richer filters on claims and history.
- Per-user submission rate limit for LIVE AI cost control.
- CSV/JSON **export of claims** for reporting.
- Email notifications (clarification request → claimant).
- Hosted deployment (Render blueprint already included).

## Next tasks suggestions
- Role-based admin for policy management.
- Claims export (CSV / JSON).
- Rate limit on `/api/claims` to cap daily LLM spend.
