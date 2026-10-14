# Prompts

One Markdown file per LLM task, loaded by the backend and web apps so prompts change without code changes and stay
provider-neutral. `risk_rationale.md` (D4) is the system prompt for the decision rationale; the user message is the JSON
hit summary (`backend/app/rationale.py`). `shopping_agent.md` (D5) is the shopping agent's
system prompt (`web/app/api/chat/route.ts`). `review_copilot.md` (D6) is the review copilot's system prompt (`web/app/api/console/chat/route.ts`; the route
appends the open order ID). D8 (stretch) adds the dispute evidence prompt.
