# Expense Claim Policy Review Assistant PRD
## Original problem statement
Build a beginner-friendly internal application reviewing employee expense claims against policy with deterministic validation and an LLM, SQLite persistence, reviewer decisions, audit history, tests, documentation, and deployment-ready packaging. Exclude reimbursement, payroll, tax advice, OCR, and payment gateways.
## Architecture decisions
- React frontend + FastAPI API + SQLite file database.
- JWT registration/login with server-side environment configuration.
- Stored policy rules are the only citation source.
- Claude Sonnet 5.5 is optional and server-side; explicit demo/fallback mode protects trust.
## Personas and requirements
- Claimant/reviewer: submit and inspect claims.
- Reviewer: approve, reject, request clarification, or override classification with an auditable reason.
- Admin/configurator: inspect sample policy and category limits.
## Implemented (2026-03-07)
Full responsive workspace, SQLite schema, validation, duplicate detection, limits, receipt checks, auth, policy evidence, AI fallback/live integration, reviewer actions, history, dashboard, tests, README, agent notes, render blueprint, and ZIP packaging.
## Prioritized backlog
- P0: replace sample rules with approved policy; configure production secrets; deploy with persistent disk.
- P1: role separation, editable policy UI, pagination, richer filters, and organization SSO.
- P2: notifications and reporting exports.