// What the chat panels say when a chat request fails (D9): the route's 429 body carries
// `rate_limited`, and a spent model budget streams `ai_unavailable` (lib/limits.ts).
export function chatErrorMessage(error: Error, who: string): string {
  if (error.message.includes("rate_limited")) {
    return "This demo limits chat messages per visitor. Please try again in a few minutes.";
  }
  if (error.message.includes("ai_unavailable")) {
    return "The demo's AI budget or rate limit is used up for now. Please try again later.";
  }
  return `${who} hit an error. Please try again.`;
}
