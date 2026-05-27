// WebSocket lifecycle. Wraps a single WebSocket and notifies the
// client via callbacks. No state machine here — that's client.ts.
//
// connectAndLogin is callable repeatedly (used both for user /login
// and the background reconnect loop). It never prints to stdout —
// the client layer decides what error to surface.

import WebSocket from 'ws';
import { CLOSE_SUPERSEDED, DeliverFrame, parseFrame } from './protocol.js';

export type OnDeliver    = (frame: DeliverFrame) => void;
export type OnDisconnect = (superseded: boolean) => void;

export type LoginResult = 'ok' | 'unreachable' | 'superseded';

export class Connection {
  private ws: WebSocket | null = null;
  private postLoginAttached: boolean = false;
  // Pending send_ok waiters keyed by message id. Resolved by the
  // post-login message handler when send_ok arrives.
  private sendOkWaiters: Map<string, (ok: boolean) => void> = new Map();

  constructor(
    private url: string,
    private onDeliver: OnDeliver,
    private onDisconnect: OnDisconnect,
  ) {}

  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN;
  }

  /** Open WS, send login, await login_ok.
   *
   * Returns one of:
   *   'ok'          — login_ok received.
   *   'unreachable' — connect failed, or WS dropped before login_ok.
   *   'superseded'  — server closed with code 4000 mid-login.
   *
   * Never prints to stdout. Callable repeatedly (reconnect loop).
   */
  connectAndLogin(name: string): Promise<LoginResult> {
    return new Promise((resolve) => {
      // Tear down any leftover socket from a prior attempt.
      if (this.ws !== null) {
        try { this.ws.close(); } catch { /* ignore */ }
        this.ws = null;
      }
      this.postLoginAttached = false;
      for (const [, w] of this.sendOkWaiters) w(false);
      this.sendOkWaiters.clear();

      const socket = new WebSocket(this.url);
      let settled = false;

      socket.on('open', () => {
        try {
          socket.send(JSON.stringify({ type: 'login', name }));
        } catch {
          if (settled) return;
          settled = true;
          resolve('unreachable');
        }
      });

      socket.on('message', (data: WebSocket.RawData) => {
        const text = Buffer.isBuffer(data) ? data.toString('utf8') : String(data);
        const frame = parseFrame(text);
        if (frame === null) return;
        if (frame.type === 'login_ok' && !settled) {
          settled = true;
          this.ws = socket;
          this.attachPostLoginHandlers(socket);
          resolve('ok');
        }
        // Other frames before login_ok: ignored.
      });

      socket.on('error', () => {
        if (settled) return;
        settled = true;
        resolve('unreachable');
      });
      socket.once('close', (code: number) => {
        if (settled) return;
        settled = true;
        resolve(code === CLOSE_SUPERSEDED ? 'superseded' : 'unreachable');
      });
    });
  }

  /** Send a frame; returns false if not connected or send fails. */
  send(frame: Record<string, unknown>): boolean {
    if (!this.isOpen || this.ws === null) return false;
    try {
      this.ws.send(JSON.stringify(frame));
      return true;
    } catch {
      return false;
    }
  }

  /** Send one `send` frame and wait for its `send_ok`. Returns true on
   * success, false on disconnect / timeout. Used by the outbox flush. */
  sendAndWaitAck(id: string, to: string, text: string, timeoutMs: number = 5000): Promise<boolean> {
    return new Promise((resolve) => {
      if (!this.isOpen) { resolve(false); return; }
      const timer = setTimeout(() => {
        this.sendOkWaiters.delete(id);
        resolve(false);
      }, timeoutMs);
      this.sendOkWaiters.set(id, (ok: boolean) => {
        clearTimeout(timer);
        resolve(ok);
      });
      const sent = this.send({ type: 'send', id, to, text });
      if (!sent) {
        clearTimeout(timer);
        this.sendOkWaiters.delete(id);
        resolve(false);
      }
    });
  }

  /** Close cleanly. */
  close(): void {
    if (this.ws !== null) {
      try { this.ws.close(1000); } catch { /* ignore */ }
      this.ws = null;
    }
    this.postLoginAttached = false;
  }

  // ─── Internals ─────────────────────────────────────────────────────

  private attachPostLoginHandlers(socket: WebSocket): void {
    if (this.postLoginAttached) return;
    this.postLoginAttached = true;

    socket.removeAllListeners('close');
    socket.removeAllListeners('error');

    socket.on('message', (data: WebSocket.RawData) => {
      const text = Buffer.isBuffer(data) ? data.toString('utf8') : String(data);
      const frame = parseFrame(text);
      if (frame === null) return;
      if (frame.type === 'deliver') {
        this.onDeliver(frame);
        try {
          socket.send(JSON.stringify({ type: 'deliver_ok', id: frame.id }));
        } catch { /* best effort */ }
      } else if (frame.type === 'send_ok') {
        const waiter = this.sendOkWaiters.get(frame.id);
        if (waiter !== undefined) {
          this.sendOkWaiters.delete(frame.id);
          waiter(true);
        }
      }
    });

    socket.on('close', (code: number) => {
      const wasOurs = this.ws === socket;
      this.ws = null;
      this.postLoginAttached = false;
      // Fail any pending send_ok waiters so flush unwinds.
      for (const [, w] of this.sendOkWaiters) w(false);
      this.sendOkWaiters.clear();
      if (wasOurs) {
        this.onDisconnect(code === CLOSE_SUPERSEDED);
      }
    });

    socket.on('error', () => { /* close will follow */ });
  }
}
