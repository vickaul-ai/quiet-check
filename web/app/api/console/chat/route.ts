import {
  convertToModelMessages,
  createUIMessageStreamResponse,
  isStepCount,
  streamText,
  toUIMessageStream,
  type UIMessage,
} from "ai";
import { z } from "zod";

import { approvalSecret, copilotPrompt, copilotTools } from "@/lib/copilot";
import { fastModel } from "@/lib/model";

// The review copilot (D6). Capture and void run only after the reviewer presses Approve (AI SDK tool approval).
const Body = z.object({
  messages: z.array(z.custom<UIMessage>()).min(1),
  order_id: z.string().regex(/^ord_[0-9A-Za-z]{1,64}$/),
});

export async function POST(req: Request) {
  const parsed = Body.safeParse(await req.json().catch(() => null));
  if (!parsed.success) {
    return Response.json({ error: { code: "validation_error", message: "Bad copilot request." } }, { status: 422 });
  }
  const { messages, order_id } = parsed.data;

  const result = streamText({
    model: fastModel(),
    instructions: `${await copilotPrompt()}\nThe reviewer has order ${order_id} open.`,
    messages: await convertToModelMessages(messages),
    tools: copilotTools(order_id),
    toolApproval: { capture_held_payment: "user-approval", void_held_payment: "user-approval" },
    experimental_toolApprovalSecret: approvalSecret(),
    stopWhen: isStepCount(5),
  });

  return createUIMessageStreamResponse({ stream: toUIMessageStream({ stream: result.stream }) });
}
