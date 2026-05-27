// Outbox file I/O.
//
// Iteration 5: append-on-queue, read-on-flush, atomic-rewrite for status
// updates (write to .tmp, then renameSync).

import { appendFileSync, existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import * as path from 'node:path';

export function outboxPath(name: string): string {
  return path.join(process.cwd(), `outbox-${name}.jsonl`);
}

export interface OutboxRow {
  id: string;
  to: string;
  text: string;
  status: 'pending' | 'sent';
}

export function appendPending(name: string, id: string, to: string, text: string): void {
  const row: OutboxRow = { id, to, text, status: 'pending' };
  appendFileSync(outboxPath(name), JSON.stringify(row) + '\n', { encoding: 'utf-8' });
}

export function readAll(name: string): OutboxRow[] {
  const p = outboxPath(name);
  if (!existsSync(p)) return [];
  const out: OutboxRow[] = [];
  for (const line of readFileSync(p, 'utf-8').split('\n')) {
    if (!line.trim()) continue;
    try {
      out.push(JSON.parse(line) as OutboxRow);
    } catch {
      // Iteration 5: ignore malformed rows. (Corruption handling → FUTURE.md.)
    }
  }
  return out;
}

export function rewriteAll(name: string, rows: OutboxRow[]): void {
  const p = outboxPath(name);
  const tmp = p + '.tmp';
  const body = rows.map((r) => JSON.stringify(r)).join('\n') + (rows.length > 0 ? '\n' : '');
  writeFileSync(tmp, body, { encoding: 'utf-8' });
  renameSync(tmp, p);
}
