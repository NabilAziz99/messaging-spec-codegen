// Wire-protocol types + parsing. Pure: no I/O, no state.

export const NAME_RE = /^[A-Za-z0-9_-]{1,32}$/;
export const CLOSE_SUPERSEDED = 4000;
export const DEFAULT_URL = 'ws://127.0.0.1:8765/ws';

// Help text. There are NO `/offline` or `/online` commands; the client
// transitions on real WebSocket events (the world of unreliable
// networks).
export const HELP_TEXT =
  '/login <name>              identify yourself\n' +
  '/send <recipient> <text>   send a message (queues if offline)\n' +
  '/help                      show this list\n' +
  '/quit                      exit';

// ─── Server-to-client frame types ──────────────────────────────────────

export interface LoginOkFrame    { type: 'login_ok';   name: string; }
export interface SendOkFrame     { type: 'send_ok';    id: string; }
export interface DeliverFrame    {
  type: 'deliver';
  id: string;
  from: string;
  to: string;
  text: string;
  server_ts: number;
}
export type ServerFrame = LoginOkFrame | SendOkFrame | DeliverFrame;

// ─── Parsing ───────────────────────────────────────────────────────────

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

export function parseFrame(raw: string): ServerFrame | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!isObject(parsed)) return null;
  const t = parsed.type;
  if (t === 'login_ok' && typeof parsed.name === 'string') {
    return { type: 'login_ok', name: parsed.name };
  }
  if (t === 'send_ok' && typeof parsed.id === 'string') {
    return { type: 'send_ok', id: parsed.id };
  }
  if (
    t === 'deliver' &&
    typeof parsed.id === 'string' &&
    typeof parsed.from === 'string' &&
    typeof parsed.to === 'string' &&
    typeof parsed.text === 'string' &&
    typeof parsed.server_ts === 'number'
  ) {
    return {
      type: 'deliver',
      id: parsed.id,
      from: parsed.from,
      to: parsed.to,
      text: parsed.text,
      server_ts: parsed.server_ts,
    };
  }
  return null;
}

export function isValidName(s: string): boolean {
  return NAME_RE.test(s);
}
