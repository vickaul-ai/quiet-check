import {
  convertToModelMessages,
  createUIMessageStreamResponse,
  isStepCount,
  streamText,
  toUIMessageStream,
  type UIMessage,
} from "ai";
import { z } from "zod";

import { MAX_QUANTITY, shoppingPrompt, shoppingTools } from "@/lib/agent";
import { fastModel } from "@/lib/model";
import { loadPersonas } from "@/lib/personas";

// The shopping agent. Runs server-side so the Anthropic key and the backend stay private.
const Body = z.object({
  messages: z.array(z.custom<UIMessage>()).min(1),
  persona: z.string(),
  cart: z.array(z.object({ sku: z.string().max(64), quantity: z.number().int().min(1).max(MAX_QUANTITY) })).max(20),
});

export async function POST(req: Request) {
  const parsed = Body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return Response.json({ error: { code: "validation_error", message: "Bad chat request." } }, { status: 422 });
  }
  const { messages, persona, cart } = parsed.data;
  // The device ID comes from personas.json, never from the browser.
  const deviceId = (await loadPersonas()).find((p) => p.key === persona)?.device_id;
  if (!deviceId) {
    return Response.json({ error: { code: "validation_error", message: "Unknown persona." } }, { status: 422 });
  }

  const result = streamText({
    model: fastModel(),
    instructions: await shoppingPrompt(),
    messages: await convertToModelMessages(messages),
    tools: shoppingTools({ cart, deviceId }),
    stopWhen: isStepCount(6),
  });

  return createUIMessageStreamResponse({ stream: toUIMessageStream({ stream: result.stream }) });
}
