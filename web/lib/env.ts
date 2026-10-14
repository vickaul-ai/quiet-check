import "server-only";

import { existsSync } from "node:fs";
import path from "node:path";

// The repo keeps one .env at its root (the backend reads the same file). Next.js only loads
// .env files from web/, so load the root one here; variables already set win.
export const REPO_DIR = path.resolve(process.cwd(), "..");
const rootEnv = path.join(REPO_DIR, ".env");
if (existsSync(rootEnv)) process.loadEnvFile(rootEnv);

// Server-only: the browser never calls the backend directly (handoff, decided 2026-10-13).
export function backendUrl(): string {
  return (process.env.BACKEND_URL || "http://127.0.0.1:8000").replace(/\/$/, "");
}

// Public by design (PayPal: "clientId is safe to put in plaintext on the client").
export function paypalClientId(): string {
  return process.env.PAYPAL_CLIENT_ID ?? "";
}
