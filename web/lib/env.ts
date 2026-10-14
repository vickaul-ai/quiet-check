import "server-only";

import { existsSync } from "node:fs";
import path from "node:path";

// The repo keeps one .env at its root (the backend reads the same file). Next.js only loads
// .env files from web/, so load the root one here; variables already set win.
export const REPO_DIR = path.resolve(process.cwd(), "..");
const rootEnv = path.join(REPO_DIR, ".env");
if (existsSync(rootEnv)) process.loadEnvFile(rootEnv);

// Server-only: the browser never calls the backend directly (handoff, decided 2026-10-13).
// Hosted (D9), the Blueprint sets BACKEND_HOST to the backend's internal hostname; Render routes
// private traffic on port 10000 to a web service's main HTTP server.
export function backendUrl(): string {
  const host = process.env.BACKEND_HOST;
  const url = process.env.BACKEND_URL || (host ? `http://${host}:10000` : "http://127.0.0.1:8000");
  return url.replace(/\/$/, "");
}

// BACKEND_API_TOKEN, shared with the backend; unset locally (D9).
export function backendToken(): string {
  return process.env.BACKEND_API_TOKEN ?? "";
}

// Public by design (PayPal: "clientId is safe to put in plaintext on the client").
export function paypalClientId(): string {
  return process.env.PAYPAL_CLIENT_ID ?? "";
}
