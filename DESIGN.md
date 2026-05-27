# Design

System-design decisions for the messaging app. Each section names a
choice, the alternative we considered, and links to the corresponding
trade-off in `FUTURE.md`.

For behavior contracts see `spec/spec.md`. For run instructions see
`README.md`.

---

## Transport: WebSocket

A long-lived WebSocket per online client. The alternative was HTTP
long-polling: simpler to write but adds latency on every push and
forces the server to track pending GETs alongside its own state.
WebSocket lets the server push `deliver` frames immediately with
zero polling overhead.

JSON-encoded frames — readable on the wire (useful for debugging),
trivial to parse in both languages with stdlib alone, no schema
compiler step.

Trade-off: [Encryption](FUTURE.md#encryption) — wire is plaintext.

## Implicit network state — no `/offline` / `/online` commands

The brief asks for a protocol that survives **unreliable networks**.
The natural read of that is: offline / online isn't something the
user decides — it's something the network does *to* them, and the
client adapts transparently.

So the client has no `/offline` or `/online` command. Instead:

- **LOGGED_IN → OFFLINE** on WebSocket close (non-4000): the reader
  loop notices the close, prints `disconnected`, and spawns a
  background task that retries connect+login with backoff (1s, 2s,
  5s, then 10s capped) indefinitely.
- **OFFLINE → LOGGED_IN** when that loop's `login_ok` arrives: print
  `reconnected`, flush the outbox.
- **LOGGED_IN → INITIAL** on close-code 4000 (supersede): name is
  no longer ours, clear it, do NOT auto-reconnect.

The user just types. `/send` while LOGGED_IN goes live; `/send` while
OFFLINE silently queues to disk. The reconnect loop handles the
recovery.

The alternative we considered was explicit `/offline` and `/online`
commands. It reads cleanly as a state machine but fights the brief's
framing — a user typing `/offline` to "tell" their client the network
died is backwards. The implicit model is also the only one that
honestly handles `kill -9` of the client process: an explicit-command
model assumes the client is alive enough to receive commands.

Trade-off: more complex client code (a background reconnect task,
abort-token bookkeeping, dual flush trigger paths). Two bugs the
implicit model invites — Python's `async for` exiting silently on
graceful close, TypeScript's reconnect-loop "running" flag never
clearing after success — are pinned in the generator recipes so
regenerations don't reproduce them.

## At-least-once delivery via inbox + deliver_ok

The server appends every accepted `send` to `inbox[recipient]` —
that's the canonical "we accepted it" step. If the recipient is
online, the server also pushes a `deliver` frame immediately. The
message stays in the inbox until a matching `deliver_ok` arrives.

If a `deliver` is pushed but the client dies before acking, the
message gets re-pushed on next login. Combined with the dedup
policy below, this yields **at-most-once user-visible delivery with
at-least-once wire delivery** — the right shape for a chat protocol
that wants no message loss.

Trade-off: [Unbounded server inbox](FUTURE.md#unbounded-server-inbox).

## Outbox = offline-queued only

Only `/send` while OFFLINE writes a row to `outbox-<name>.jsonl`.
Live sends (while LOGGED_IN) go straight to the wire — they're never
persisted. The alternative was "persist every send for crash
recovery," which inflates the file with messages the server has
already accepted.

Result: the outbox file grows with frequency of offline periods, not
with total message volume.

Trade-off: [Mid-send-disconnect](FUTURE.md#mid-send-disconnect) —
live sends that die mid-flight are lost.

## UUID dedup at the server

Every `send` carries a sender-generated UUID v4. The server tracks
`seen_ids[sender]` and silently dedups retries — same `(sender, id)`
arriving twice means the second is dropped (but still gets `send_ok`
so the sender can mark its outbox row sent).

This makes the outbox flush **idempotent**: the client can safely
replay every `pending` row on reconnect without risking duplicate
delivery. The retry policy on a flaky connection becomes trivial —
just resend until the row is `status: sent`.

## Strict frame validation

Frames with unknown frame types, missing required fields, or extra
top-level keys are rejected (WebSocket close code 4001). The
alternative was "ignore unknown fields" — softer on forward compat
but lets a malformed sender go undetected. Strict validation catches
the bug at the boundary.

Trade-off: [Strict validation](FUTURE.md#strict-validation) — future
protocol versions need explicit handshake to extend.

## Single-threaded event loop, both sides

Python `asyncio`, Node async/await. One event at a time — no shared
mutable state to guard. The reconnect interleaving (server flushing
its inbox while the client flushes its outbox, on the same socket)
is simple to reason about because each side processes inbound and
outbound serially within its loop.

The alternative — multi-threaded handlers — buys nothing for a
single-process server with O(connected users) state and adds locks
the spec doesn't need.

## Auto-register on first login

`/login alice` claims the name "alice" iff it's not already taken
by another live connection. No signup step, no password. Defensible
because the brief excludes security entirely.

Trade-off: [Authentication](FUTURE.md#authentication).

## Single connection per name (supersede)

If a second connection logs in with a name that's already connected,
the server closes the first with close code 4000 reason `superseded`.
The client receiving the 4000 transitions to INITIAL and prints
`disconnected: superseded by another session`.

The alternative was "reject the second login while the first is live"
— but that locks out the legitimate user if their previous session
died abnormally and the connection hasn't timed out yet. Supersede
favors recovery over guarding.

Trade-off: [Multi-device per user](FUTURE.md#multi-device-per-user).

## Test harness: subprocess + per-iteration case modules

Clients are in different languages, so the harness drives both via
stdin/stdout over subprocess — the only portable mechanism. Cases
live in `test/cases/case_NN_*.py`, each module exposing a `cases()`
function the runner concatenates in order.

Each test boots its own server in a context manager (clean state per
case), spawns clients in temp directories (outbox files don't collide),
and asserts both stdout content and on-disk outbox state.

## Testing unreliable networks

Each critical behavior of the implicit model gets a dedicated test,
because a half-correct implementation would silently pass without one:

- **WS-drop → OFFLINE** (Step 3.a) — `server.stop()` mid-test, then
  `alice.wait_for("disconnected")`. Catches the bug where Python's
  `async for raw in ws` exits silently on graceful close and the
  client stays LOGGED_IN forever (we hit this and pinned it in the
  generator recipe).

- **Supersede halts the reconnect loop** (Step 3.d, *the correctness
  gate*) — after a successful auto-reconnect, kick alice with a
  second `/login alice` from a different client, then
  `alice.assert_not_seen("reconnected", window=1.5)`. Without this
  check you could ship a client with a retry timer still ticking
  after 4000 — looks healthy but leaks work forever.

- **Outbox flush on every LOGGED_IN entry** — Step 5.a exercises the
  auto-reconnect path (in-process recovery); Step 7 exercises the
  fresh-`/login` path via `restart_client()`, proving a
  killed-and-relaunched process recovers pending sends from disk.
  Without the second test, the spec's "flush on every LOGGED_IN
  entry" rule is half-verified.

- **`deliver_ok` clears the server inbox** (Step 6.c) — bob receives,
  server cycle, bob auto-reconnects; `bob.assert_not_seen(msg, 0.4)`.
  Without this the server would re-deliver the message on every
  reconnect.

- **Brief's full 7-step scenario** (Step 7) — hybrid:
  `server.stop()/.start()` for the global-outage phase, then
  `ClientHandle.kill()` + `restart_client()` for the asymmetric
  phase where alice and bob are independently offline. Asserts the
  full final state on both clients' terminals, both outbox files,
  and both server inboxes.

Two harness primitives unlock all of these: `ServerHandle.stop()/.start()`
for global outages, and `ClientHandle.kill()` + `restart_client(prev,
cmd)` for per-client outages with the on-disk outbox surviving. The
rejected alternative for per-client drops was a server-side
`/admin/kick` endpoint — the kill+restart path is stronger because
it also exercises the "process died, came back later" recovery,
which the in-process auto-reconnect path alone doesn't cover.
