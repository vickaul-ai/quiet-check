# Prompts

One Markdown file per LLM task, loaded by the backend and web apps so prompts change without code changes and stay
provider-neutral. `risk_rationale.md` (D4) is the system prompt for the decision rationale; the user message is the JSON
hit summary (`backend/app/rationale.py`). D6 adds the copilot prompt; D8 (stretch) adds the dispute evidence prompt.
