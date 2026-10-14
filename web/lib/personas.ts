import "server-only";

import { readFile } from "node:fs/promises";
import path from "node:path";

import { REPO_DIR } from "./env";
import type { Persona } from "./types";

// backend/app/personas.json is the source of truth (D7). Only the public fields leave the server.
export async function loadPersonas(): Promise<Persona[]> {
  const raw = await readFile(path.join(REPO_DIR, "backend/app/personas.json"), "utf8");
  return (JSON.parse(raw) as Persona[]).map(({ key, name, device_id, status }) => ({ key, name, device_id, status }));
}
