import { createHash } from "node:crypto";
import { mkdir, copyFile, stat } from "node:fs/promises";
import path from "node:path";

export function cacheKey(prompt: string, aspect: string, refs: string[]): string {
  const h = createHash("sha256");
  h.update(prompt);
  h.update("|");
  h.update(aspect);
  h.update("|");
  for (const r of [...refs].sort()) h.update(r);
  return h.digest("hex").slice(0, 16);
}

export async function lookupCache(cacheDir: string, key: string): Promise<string | null> {
  const entry = path.join(cacheDir, `${key}.png`);
  try {
    const s = await stat(entry);
    if (s.size > 1000) return entry;
  } catch {}
  return null;
}

export async function storeCache(cacheDir: string, key: string, sourcePath: string): Promise<void> {
  await mkdir(cacheDir, { recursive: true });
  const entry = path.join(cacheDir, `${key}.png`);
  await copyFile(sourcePath, entry);
}
