# Design

System-design decisions for the messaging app. Every section is a
choice I made; Claude wrote the prose on my outlines.

For behavior contracts see `spec/spec.md`. For run instructions and
the explicit me-vs-Claude authorship boundary see `README.md`.

---

## Transport: WebSocket, JSON frames

One long-lived WebSocket per online client. Server pushes `deliver`
frames immediately with no polling. JSON on the wire — readable,
stdlib-parseable in both languages, no schema compiler step.

Trade-off: [Encryption](FUTURE.md#encryption) — wire is plaintext.

## Module structure

**Server (3 modules)**, split by what kind of mutation they do:

- `server/protocol.py` — pure validation, no I/O, no state. Owns the
  frame-shape check and the close-code constants.
- `server/handlers.py` — module-level state (`connections`, `inbox`,
  `seen_ids`) and per-frame handlers. State is mutated directly;
  the single-threaded asyncio model means no locks.
- `server/server.py` — WebSocket lifecycle only. Path check, frame
  loop, dispatch by type, cleanup in `finally`. No state of its own.

**Client (4 modules, ~150-200 LoC each)** — pinned in the generator
recipe:

- `protocol` — wire format, frame types, help text, constants.
- `outbox` — append-on-queue, read-on-flush, atomic rewrite. Knows
  nothing about the WebSocket.
- `connection` — the WebSocket. Owns the reader loop and the per-id
  `send_ok` waiters. No state machine, no commands.
- `client` — state machine, command dispatcher, background reconnect
  loop, outbox flush. Glue.

Why 4 modules and not 1: keeps each file diffable across
regenerations. A single-file client makes LLM-local rewrites
unpredictable — regenerating one part subtly shifts the rest. The
recipe explicitly rejects single-file submissions.

## Network-driven state machine

Three client states: `INITIAL`, `LOGGED_IN`, `OFFLINE`. Transitions
are driven by WebSocket events:

- WS close with code ≠ 4000 (`LOGGED_IN` → `OFFLINE`): reader loop
  catches it, prints `disconnected`, spawns a background task that
  retries connect+login indefinitely with exponential backoff
  (1s, 2s, 5s, then 10s capped).
- `login_ok` from that loop (`OFFLINE` → `LOGGED_IN`): print
  `reconnected`, flush the outbox.
- WS close with code 4000, supersede (`LOGGED_IN` → `INITIAL`):
  clear the cached name, do NOT auto-reconnect.

`/send` while `LOGGED_IN` goes live to the wire. `/send` while
`OFFLINE` queues to disk and prints `queued`. The reconnect loop
owns the recovery — the user issues no explicit reconnect command.

Three implementation patterns the recipe pins because half-correct
versions fail conformance silently:

1. **`connect_and_login` returns a 3-way result**
   (`'ok' | 'unreachable' | 'superseded'`), not a bool. The user
   `/login` path collapses both failure modes into one error string;
   the reconnect loop distinguishes them — `'unreachable'` triggers
   another backoff, `'superseded'` terminates the loop.
2. **Reader-loop close-handler invariant.** The handler must fire on
   *every* loop termination — silent `async for` exit, explicit
   `ConnectionClosed`, anything else. Older `websockets` versions
   exited silently on graceful close; the first implementation only
   handled the exception branch and the client stayed `LOGGED_IN`
   forever. Pinned in the Python recipe item #10.
3. **Reconnect "running" flag must self-clear.** The TypeScript
   reconnect loop's abort token is set on spawn and must be cleared
   when the loop exits (via `.finally()`). Without that, a one-shot
   successful reconnect leaves the flag set and the next disconnect
   short-circuits with "already running". Pinned in the TS recipe
   item #10.

Outbox flush runs on **every** `LOGGED_IN` entry — both the
auto-reconnect path and fresh `/login` after a process restart. The
killed-and-relaunched recovery only works because of this.

Trade-off: more complex client code than a synchronous request/reply
model would need.

## At-least-once delivery via inbox + `deliver_ok`

Server appends every accepted `send` to `inbox[recipient]` — that's
the canonical "we accepted it" step. If the recipient is online, the
server also pushes a `deliver` immediately. Messages stay in the
inbox until `deliver_ok` arrives.

Yields at-most-once user-visible delivery with at-least-once wire
delivery — the right shape for a chat protocol that wants no
message loss.

Trade-off: [Unbounded server inbox](FUTURE.md#unbounded-server-inbox).

## Outbox: offline-queued only, atomic on update

JSON Lines, one row per line, UTF-8, explicit LF newlines. Path:
`./outbox-<name>.jsonl`. Schema is strict — exactly four keys:
`id`, `to`, `text`, `status` (`"pending"` or `"sent"`).

Append on `/send` while `OFFLINE`. Never written on `LOGGED_IN`
sends — those go straight to the wire. Status flips
`"pending"` → `"sent"` when the matching `send_ok` arrives.

Status updates can't be partial-write-truncated: write the new file
to `<path>.tmp`, then atomic-rename over the original (`os.replace`
on Python, `renameSync` on Node — both atomic on POSIX and Windows).

Result: outbox grows with offline periods, not with total message
volume.

Trade-off: [Mid-send-disconnect](FUTURE.md#mid-send-disconnect) —
live sends that die mid-flight are lost (no in-memory pending set).

## UUID dedup at the server — key is `(sender, id)`

Every `send` carries a sender-generated UUID v4. Server tracks
`seen_ids[sender]` and silently drops retries. A duplicate still
receives `send_ok` so the client can flip its outbox row to `"sent"`
and stop retrying.

**Key choice is `(sender, id)`, not `id` alone.** Two senders can in
principle generate the same UUID; only the sender's namespace
matters. The same `(sender, id)` with different `to` or `text` is
still treated as a duplicate (first-write-wins) — defends against an
adversarial client recycling an id to multiple recipients. Tested by
Step 8.b.

The dedup makes the outbox flush idempotent end-to-end. The client
can safely replay every pending row on reconnect without risk of
duplicate delivery.

## Strict frame validation

`parse_frame` rejects on any wire-shape violation: non-parseable
JSON, non-object body, unknown `type`, missing required fields,
*and* extra top-level keys. Each violation maps to a deterministic
close-reason string (`invalid json`, `invalid frame`, `oversize
frame`, plus per-handler causes like `send: not logged in`). Frame
size cap (16 KiB) is enforced before parsing.

Set-equality on the key set (not subset) is the key call: a frame
with extras is rejected, not silently accepted with extras ignored.
Tested by Steps 9.a–9.d.

Trade-off: [Strict validation](FUTURE.md#strict-validation) —
future protocol versions need an explicit handshake to extend the
schema without breaking older peers.

## Single-threaded event loop, both sides

Python `asyncio`, Node async/await. One event at a time, no shared
mutable state to guard, no locks. The reconnect interleaving (server
flushing its inbox while the client flushes its outbox on the same
socket) is simple to reason about because each side processes
inbound and outbound serially within its loop.

## Auto-register on first login

`/login alice` claims the name iff not currently in use. No signup,
no password — the brief excludes security entirely.

Trade-off: [Authentication](FUTURE.md#authentication).

## Single connection per name (supersede)

A second `/login alice` while the first connection is live closes
the first with code 4000. Favors recovery over guarding — if the
previous session died abnormally and the TCP connection hasn't
timed out yet, the legitimate user is not locked out.

Trade-off: [Multi-device per user](FUTURE.md#multi-device-per-user).

## Testing unreliable networks

Two harness primitives in `test/framework.py` drive every network
scenario:

- **`ServerHandle.stop()` / `.start()`** — bounce the server
  mid-test. Stop sends SIGINT, waits up to 3s, falls back to
  SIGKILL, then sleeps 150 ms so the OS releases the listen socket
  before the next `.start()`. Every connected client's WS dies at
  once — drives global outage.
- **`ClientHandle.kill()` + `restart_client(prev, cmd)`** — SIGKILL
  one client and respawn in the same cwd. On-disk outbox persists;
  the relaunched client flushes it on the next `/login`. Drives
  per-client outage with state recovery.

Two assertion helpers carry weight:

- `wait_for(expected, timeout)` advances a per-handle cursor past
  each match — repeated `wait_for("queued")` calls find each NEW
  occurrence, not the first one over and over.
- `assert_not_seen(forbidden, window)` is **forward-looking** —
  baseline = current line count, sleep, then scan only lines added
  during the window. The right shape for "X must not appear in the
  next N seconds." Powers the Step 3.d correctness gate.

Five tests are the design's correctness gates:

| Behavior | Test | Mechanism |
|---|---|---|
| WS-drop → OFFLINE | Step 3.a | `server.stop()` + `wait_for("disconnected")` |
| Supersede halts reconnect | Step 3.d | Kick alice from a second client + `assert_not_seen("reconnected", 1.5)` |
| Outbox flush on every LOGGED_IN entry | Step 5.a + Step 7 | Step 5.a hits auto-reconnect path; Step 7 hits fresh-login path via `restart_client()` |
| `deliver_ok` clears the inbox | Step 6.c | Server cycle after bob acks + `assert_not_seen(msg, 0.4)` |
| Brief's 7-step scenario | Step 7 | Hybrid: server cycle for global phase + `kill()` + `restart_client()` for asymmetric phase |

Authorship per test file is documented in `README.md` § "Test files —
authorship by file".
