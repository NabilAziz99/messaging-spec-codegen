// Messaging CLI client (TypeScript) — entry point + commands + state.
//
// State is driven by network events, not user commands. There is no
// /offline / /online — the WebSocket dies, we drop to OFFLINE and
// print `disconnected`, then a background task auto-reconnects with
// backoff. When `login_ok` comes back we print `reconnected` and
// flush the outbox.

import readline from 'node:readline';
import { randomUUID } from 'node:crypto';
import {
  DEFAULT_URL,
  DeliverFrame,
  HELP_TEXT,
  isValidName,
} from './protocol.js';
import { Connection } from './connection.js';
import { appendPending, readAll, rewriteAll } from './outbox.js';

type State = 'INITIAL' | 'LOGGED_IN' | 'OFFLINE';

// Auto-reconnect backoff schedule (ms). After the list is exhausted,
// every subsequent attempt waits BACKOFF_CAP_MS. Pinned so test
// timings are deterministic.
const BACKOFF_SCHEDULE_MS: number[] = [1000, 2000, 5000];
const BACKOFF_CAP_MS = 10000;

function backoffForAttempt(attempt: number): number {
  if (attempt < BACKOFF_SCHEDULE_MS.length) return BACKOFF_SCHEDULE_MS[attempt];
  return BACKOFF_CAP_MS;
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

class Client {
  state: State = 'INITIAL';
  name: string | null = null;
  conn: Connection;
  // Owns the background reconnect loop. Null when no loop is active.
  private reconnectAbort: { aborted: boolean } | null = null;

  constructor() {
    const url = process.env.MSG_SERVER || DEFAULT_URL;
    this.conn = new Connection(
      url,
      (frame: DeliverFrame) => this.onDeliver(frame),
      (superseded: boolean) => this.onDisconnect(superseded),
    );
  }

  // ─── Connection callbacks ──────────────────────────────────────────

  private onDeliver(frame: DeliverFrame): void {
    // Connection.attachPostLoginHandlers sends the deliver_ok on the
    // wire side; the client layer only owns the user-visible line.
    console.log(`[${frame.from} → ${frame.to}]  ${frame.text}`);
  }

  private onDisconnect(superseded: boolean): void {
    // Reader callback: WS just closed unexpectedly.
    //  - 4000 (superseded) → terminal: clear name, go INITIAL.
    //  - any other close while LOGGED_IN → transient: go OFFLINE,
    //    spawn the reconnect loop.
    //  - close while already OFFLINE/INITIAL → ignore (reconnect's
    //    own attempts close the WS as they retry).
    if (this.state !== 'LOGGED_IN') return;
    if (superseded) {
      console.log('disconnected: superseded by another session');
      this.state = 'INITIAL';
      this.name = null;
      return;
    }
    console.log('disconnected');
    this.state = 'OFFLINE';
    this.startReconnectLoop();
  }

  // ─── Auto-reconnect loop ───────────────────────────────────────────

  private startReconnectLoop(): void {
    if (this.reconnectAbort !== null && !this.reconnectAbort.aborted) {
      // Already running.
      return;
    }
    const token = { aborted: false };
    this.reconnectAbort = token;
    // Clear the abort token after the loop exits (success or otherwise)
    // so a subsequent disconnect can spawn a fresh loop. Without this,
    // a one-shot successful reconnect leaves the token in a "running"
    // state and the next disconnect's startReconnectLoop early-returns.
    void this.runReconnectLoop(token).finally(() => {
      if (this.reconnectAbort === token) this.reconnectAbort = null;
    });
  }

  private async runReconnectLoop(token: { aborted: boolean }): Promise<void> {
    /* Background task: retry connect+login forever (or until 4000).
     *
     * Spec invariants:
     *  - Backoff schedule: 1s, 2s, 5s, then 10s capped.
     *  - Cached name persists; we re-login as it.
     *  - On 4000 mid-attempt → terminal: print supersede line, go
     *    INITIAL, clear name, exit.
     *  - On success → print `reconnected`, flush outbox, exit.
     */
    let attempt = 0;
    const cached = this.name;
    if (cached === null) return;
    while (this.state === 'OFFLINE' && !token.aborted) {
      await sleep(backoffForAttempt(attempt));
      attempt += 1;
      if (this.state !== 'OFFLINE' || token.aborted) return;
      const result = await this.conn.connectAndLogin(cached);
      if (token.aborted) return;
      if (result === 'ok') {
        this.state = 'LOGGED_IN';
        console.log('reconnected');
        await this.flushOutbox();
        return;
      }
      if (result === 'superseded') {
        console.log('disconnected: superseded by another session');
        this.state = 'INITIAL';
        this.name = null;
        return;
      }
      // 'unreachable' — try again after the next backoff.
    }
  }

  // ─── Commands ──────────────────────────────────────────────────────

  async cmdLogin(args: string[]): Promise<void> {
    if (this.state === 'LOGGED_IN' || this.state === 'OFFLINE') {
      console.log(`error: already logged in as ${this.name}`);
      return;
    }
    if (args.length !== 1 || !isValidName(args[0])) {
      console.log('error: usage: /login <name>');
      return;
    }
    const name = args[0];
    const result = await this.conn.connectAndLogin(name);
    if (result !== 'ok') {
      // 'unreachable' or 'superseded' — both surface as the same
      // user-facing error on a user-initiated /login: we never
      // claimed the name.
      console.log('error: cannot reach server');
      return;
    }
    this.name = name;
    this.state = 'LOGGED_IN';
    console.log(`logged in as ${name}`);
    // Spec: outbox flushes on every LOGGED_IN entry. Covers the
    // killed-and-relaunched case where pending rows are on disk.
    await this.flushOutbox();
  }

  async cmdSend(lineAfterVerb: string): Promise<void> {
    if (this.state === 'INITIAL') {
      console.log('error: not logged in; use /login <name> first');
      return;
    }
    if (!lineAfterVerb.startsWith(' ')) {
      console.log('error: usage: /send <recipient> <text>');
      return;
    }
    const rest = lineAfterVerb.slice(1);
    const wsIdx = rest.search(/\s/);
    if (wsIdx === -1) {
      console.log('error: usage: /send <recipient> <text>');
      return;
    }
    const recipient = rest.slice(0, wsIdx);
    const text = rest.slice(wsIdx + 1);
    if (!recipient || !text || !isValidName(recipient)) {
      console.log('error: usage: /send <recipient> <text>');
      return;
    }

    const id = randomUUID();

    if (this.state === 'OFFLINE') {
      // Queue to disk; don't transmit. The reconnect loop will flush
      // this row once login_ok arrives.
      if (this.name === null) {
        console.log('error: not logged in; use /login <name> first');
        return;
      }
      appendPending(this.name, id, recipient, text);
      console.log('queued');
      return;
    }

    // LOGGED_IN — transmit immediately, do NOT write to outbox.
    this.conn.send({ type: 'send', id, to: recipient, text });
  }

  private async flushOutbox(): Promise<void> {
    if (this.name === null) return;
    const rows = readAll(this.name);
    if (rows.length === 0) return;
    for (let i = 0; i < rows.length; i++) {
      const row = rows[i];
      if (row.status !== 'pending') continue;
      const ok = await this.conn.sendAndWaitAck(row.id, row.to, row.text);
      if (!ok) {
        // Connection died or timed out; row stays pending for next reconnect.
        return;
      }
      rows[i] = { ...row, status: 'sent' };
      rewriteAll(this.name, rows);
    }
  }

  cmdHelp(): void {
    console.log(HELP_TEXT);
  }

  // ─── Top-level loop ────────────────────────────────────────────────

  async handleLine(line: string): Promise<boolean> {
    const trimmed = line.replace(/[\r\n]+$/, '');
    if (!trimmed) return true;
    if (!trimmed.startsWith('/')) {
      console.log('error: unknown command — try /help');
      return true;
    }
    const sp = trimmed.indexOf(' ');
    const verb = sp === -1 ? trimmed : trimmed.slice(0, sp);
    const rest = sp === -1 ? '' : trimmed.slice(sp);

    if (verb === '/login') {
      const args = rest.trim() ? rest.trim().split(/\s+/) : [];
      await this.cmdLogin(args);
    } else if (verb === '/send') {
      await this.cmdSend(rest);
    } else if (verb === '/help') {
      if (rest.trim()) console.log('error: usage: /help');
      else this.cmdHelp();
    } else if (verb === '/quit') {
      if (this.reconnectAbort !== null) this.reconnectAbort.aborted = true;
      this.conn.close();
      return false;
    } else {
      console.log('error: unknown command — try /help');
    }
    return true;
  }

  async run(): Promise<void> {
    const rl = readline.createInterface({ input: process.stdin });
    for await (const line of rl) {
      if (!(await this.handleLine(line))) break;
    }
    if (this.reconnectAbort !== null) this.reconnectAbort.aborted = true;
    this.conn.close();
  }
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);
  if (args.includes('--help')) { console.log(HELP_TEXT); return; }
  if (args.includes('--version')) { console.log('messaging-client 1.0.0'); return; }
  if (args.length > 0) {
    console.log(`error: unknown argument ${args[0]}`);
    process.exit(1);
  }
  await new Client().run();
  process.exit(0);
}

main().catch((err) => {
  console.error(`fatal: ${err}`);
  process.exit(1);
});
