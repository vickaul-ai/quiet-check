import "server-only";

import { createAnthropic } from "@ai-sdk/anthropic";

import "./env";

// The one place the web app creates an LLM (docs/brief.md, "Models"). Swap providers here.
export function fastModel() {
  const provider = process.env.LLM_PROVIDER || "anthropic";
  const model = process.env.LLM_MODEL_FAST || "claude-haiku-5-5";
  if (provider !== "anthropic") {
    throw new Error(`LLM_PROVIDER=${provider} isn't wired up in web/lib/model.ts`);
  }
  return createAnthropic({ apiKey: process.env.ANTHROPIC_API_KEY })(model);
}
