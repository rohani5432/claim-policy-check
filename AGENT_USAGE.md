# AGENT_USAGE.md

## Tools and models used
- A design planning agent produced `/app/design_guidelines.json`; the interface follows its Swiss / high-contrast direction with teal and terracotta accents.
- An integration playbook was consulted for `emergentintegrations.llm.chat.LlmChat` and for the JWT-style reviewer authentication wiring.
- The live analysis model is `anthropic/claude-sonnet-5-5`, called non-streaming through `LlmChat.send_message` so the response can be parsed as a single JSON object. The Emergent universal LLM key is read from `EMERGENT_LLM_KEY` on the backend only.

## Representative prompt
The backend sends a single user message containing JSON with three top-level keys:
- `claim`: the submitted expense fields.
- `policy_rule`: the single resolved policy rule (code, title, text, category, limit_amount) from SQLite.
- `deterministic_findings`: the output of the server-side validator.

The system message instructs the model to return a single JSON object with `classification`, `confidence`, `uncertain`, `decision` (one of `Pending Review` / `Needs Human Review`), `explanation`, and `questions[]`; to never invent policy rules; and to mark uncertainty when a deterministic finding has severity `review`.

## Delegated work and verification
- Design planning and integration playbook retrieval were delegated to the available planning agents; application code was written here.
- Automated backend tests: 13 offline pytest cases in `backend/test_server.py` (demo-mode so no AI credits are spent) plus a live end-to-end suite that calls the preview backend to verify:
  - `/api/health` reports `ai_mode="LIVE AI"` when the key is configured.
  - A within-limit claim submission produces `ai.mode="LIVE AI"` with the stored rule as the only citation.
  - A deterministic `LIMIT-EXCEEDED` finding still forces the overall status to `Needs Human Review` even when the live model answers.
  - Policy CRUD, duplicate code 409, JSON upsert import, and CSV import dry-run / transactional abort on invalid rows.
  - `policy:create` / `update` / `delete` / `import` entries land in `/api/history`.
- A frontend screenshot verification confirmed the Policy Admin page lists seeded rules and exposes Add / Edit / Delete / Preview / Apply import controls.
- No hosted deployment URL is claimed. Deployment was intentionally skipped for this iteration.

## Rejected / corrected suggestions
- The initial AI call used streaming (`stream_message`) and only logged a generic error. Changed to non-streaming `send_message` for structured JSON, with an explicit `DEMO MODE` / `DEMO FALLBACK` label so a provider failure is never passed off as a real analysis.
- The seed `POLICY` list used to be a module-level constant used directly by the validator. Rejected keeping it that way once policy editing was requested: the validator now reads from the SQLite `policy` table so edits and imports take effect immediately.
- A suggestion to let the model override deterministic flags was rejected. The server coerces `decision` to `Needs Human Review` whenever any deterministic finding has severity `review`, regardless of what the model returned.
