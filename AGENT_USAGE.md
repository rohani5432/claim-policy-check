# AGENT_USAGE.md
## Tools and models
- A design planning agent generated `/app/design_guidelines.json`; the interface follows its Swiss/high-contrast direction with teal and terracotta accents.
- An integration playbook was consulted for `emergentintegrations`, streaming Claude usage, and custom authentication guidance.
- The selected live model is `anthropic/claude-sonnet-5-5`.
## Representative prompt
The backend sends the claim, selected stored policy object, and deterministic validation results with a system instruction to return structured JSON, cite only supplied policy text, identify uncertainty, and never auto-approve uncertain cases.
## Delegated work and verification
The design and integration guidance above were delegated to the available planning agents. Application code was generated in this workspace. A focused pytest suite covers validation, fallback labelling, review actions, and history persistence. No live AI call or hosted deployment is claimed unless credentials and provider setup are completed.