# Messaging app — behavior spec

This is the **source of truth**. Two independent client implementations
in different languages, generated against this spec, must
interoperate. Where the generated code disagrees with the spec, the
spec wins.

This spec is built iteratively. **All 10 iterations complete.**

---

## Overview

A minimal messaging system: one server, two CLI clients. Users
identify themselves by name and exchange text messages. Clients
connect to the server over WebSocket; they exchange JSON frames.
There is no security and no persistence (in-memory server, ephemeral
client state).

## Wire protocol

**Transport.** WebSocket. Endpoint: `ws://127.0.0.1:8765/ws`. Text
frames, UTF-8, one JSON object per frame.

**Frames currently defined:**

| Direction | Type | Fields |
|---|---|---|
| C→S | `login` | `name: string` |
| S→C | `login_ok` | `name: string` |
| C→S | `send` | `id: string (UUID v4)`, `to: string`, `text: string` |
| S→C | `send_ok` | `id: string` |
| S→C | `deliver` | `id: string`, `from: string`, `to: string`, `text: string`, `server_ts: int` |
| C→S | `deliver_ok` | `id: string` |

Examples:

```json
// C→S: login
{"type": "login", "name": "alice"}

// S→C: login_ok
{"type": "login_ok", "name": "alice"}

// C→S: send
{"type": "send", "id": "550e8400-e29b-41d4-a716-446655440000", "to": "bob", "text": "hi"}

// S→C: send_ok
{"type": "send_ok", "id": "550e8400-e29b-41d4-a716-446655440000"}

// S→C: deliver
{"type": "deliver", "id": "550e8400-e29b-41d4-a716-446655440000",
 "from": "alice", "to": "bob", "text": "hi", "server_ts": 1716643200}

// C→S: deliver_ok
{"type": "deliver_ok", "id": "550e8400-e29b-41d4-a716-446655440000"}
```

**Name validation.** `^[A-Za-z0-9_-]{1,32}$` — ASCII letters, digits,
underscore, hyphen. Length 1-32. Case-sensitive.

**Message ID validation.** UUID v4, lowercase 36 chars
(`8-4-4-4-12` hex with hyphens).

**`server_ts`.** Integer seconds since Unix epoch, set by server at
the moment it accepts the `send`.

## Server behavior

**In-memory state:**

- `connections: dict[name, WebSocket]` — currently-connected users.
- `inbox: dict[name, list[Message]]` — per-user pending messages
  awaiting delivery. Each `Message` is the body of a `deliver` frame
  (id, from, to, text, server_ts).
- `seen_ids: dict[name, set[id]]` — per-sender set of message ids the
  server has already accepted. Used for dedup so retried `send` frames
  don't double-deliver.

**On `login` frame:**

1. Validate `name` against the name pattern. If invalid, close with
   WebSocket close code 4001.
2. If `connections[name]` already exists, close that older connection
   with code 4000 reason `"superseded"`.
3. Register: `connections[name] = ws`.
4. Send `{"type": "login_ok", "name": name}` to the new connection.
5. **Flush** `inbox[name]` — for each pending message, send a
   `deliver` frame to the new connection in FIFO order. Messages
   remain in the inbox until `deliver_ok` removes them.

**On disconnect.** Remove `connections[name]` if it points to this
socket. The inbox is **not** touched (un-acked messages will be
re-delivered on the next login).

**On `send` frame:**

1. Validate fields: `id` is a UUID v4 string; `to` matches the name pattern;
   `text` is a string.
2. Determine sender from the connection's registered user. If the
   connection isn't logged in, close with 4001.
3. **Dedup check.** If `id ∈ seen_ids[sender]`, this is a retry —
   silently drop. Skip steps 4-6 but still send `send_ok {id}` so the
   client can mark its outbox row as `sent`. **Dedup is on `(sender, id)`
   alone:** if the same `(sender, id)` arrives twice with different
   `to` or `text`, the second is still treated as a duplicate and
   never stored. The first one's content is authoritative.
4. Set `server_ts = int(time.time())`.
5. **Append to `inbox[to]`** (creating the list if needed). This is
   the canonical "we accepted the message" step.
6. Record `seen_ids[sender].add(id)`.
7. If `connections[to]` exists, **also** push a `deliver` frame to
   that connection immediately. (The message still sits in the inbox
   until `deliver_ok` removes it — at-least-once delivery.)
8. Send `{"type": "send_ok", "id": id}` to the sender.

**On `deliver_ok` frame:**

Find the matching `id` in the connection's owner's inbox and remove it.
If the id isn't in the inbox (already removed, or never was), silently
ignore.

## Client behavior

**Design center: unreliable networks.** OFFLINE / online state is
**not** driven by user commands — it's a reaction to the actual
WebSocket connection. The user types and the client transparently
decides whether to transmit on the wire or queue locally. This
matches how real chat apps behave (the network dies; the user
doesn't know; messages queue; the connection comes back; everything
flushes).

**State machine.** Three states, transitions driven by network events:

```
                /login <name>          ws closes (non-4000)
   INITIAL ──────────────────► LOGGED_IN ──────────────────► OFFLINE
       ▲                           ▲                            │
       │                           │   reconnect + login_ok     │
       │                           └────────────────────────────┘
       │
       │  ws closes with code 4000 (superseded — name cleared)
       └─────────────────────────────────────────────────────────
```

**Commands accepted in INITIAL:** `/login`, `/help`, `/quit`.
**Commands accepted in LOGGED_IN:** `/send`, `/help`, `/quit`.
**Commands accepted in OFFLINE:** `/send` (queues — Iter 4), `/help`, `/quit`.

OFFLINE is **transient** — the client runs a reconnect loop in the
background and re-enters LOGGED_IN as soon as the network is back.
The cached `name` persists across OFFLINE.

On WebSocket close code 4000 (superseded), the client goes back to
**INITIAL** (the cached name is no longer ours — it belongs to whoever
just logged in as us). No auto-reconnect; the user must `/login`
again to claim a new identity.

**`/login <name>` behavior:**

1. Validate `name` locally. If invalid → print `error: usage: /login <name>`, stay INITIAL.
2. If already LOGGED_IN → print `error: already logged in as <current_name>`, do nothing.
3. Open a WebSocket to the server. If connection fails → print `error: cannot reach server`, stay INITIAL.
4. Send `{"type": "login", "name": <name>}`.
5. Wait for `login_ok`.
6. Transition INITIAL → LOGGED_IN. Print `logged in as <name>`.
7. **Flush the outbox** (see "Outbox flush" below). A relaunched client
   may find pending rows on disk from a prior session — they go out
   as soon as we have a connection.

**`/send <recipient> <text>` behavior (LOGGED_IN only):**

1. If INITIAL → print `error: not logged in; use /login <name> first`.
2. Parse line: `recipient` is the first whitespace-separated token after `/send`;
   `text` is the rest of the line (after a single separator space, trim trailing
   newline only — preserve internal whitespace).
3. Validate: recipient matches the name pattern; text is non-empty.
   If invalid → print `error: usage: /send <recipient> <text>`.
4. Generate a UUID v4 (`id`).
5. Send `{"type": "send", "id": id, "to": recipient, "text": text}`.
6. No status output on success — the recipient's `deliver` is the global signal.

**On inbound `deliver` frame:**

1. Print `[<from> → <to>]  <text>` (exact: two spaces between `]` and the text).
2. Send `{"type": "deliver_ok", "id": <id>}` back to the server.

**Network state transitions (implicit — driven by the WebSocket):**

1. **On WebSocket close while LOGGED_IN, code != 4000** (server died,
   network dropped, abnormal closure):
   - Transition LOGGED_IN → OFFLINE.
   - Print `disconnected` (one line, once per transition).
   - Start the **reconnect loop** in the background:
     - Try to connect. On failure, wait (exponential backoff: 1s,
       2s, 5s, capped at 10s) and retry. Retry indefinitely.
     - On TCP/WS connect success → send `{"type": "login", "name": <cached_name>}`.
     - On `login_ok` → transition OFFLINE → LOGGED_IN. Print `reconnected`. Flush the outbox (see "Outbox flush" below).
     - On `login_ok` failure or close 4000 during reconnect → transition to INITIAL (cached name lost). Print `disconnected: superseded by another session`.

2. **On WebSocket close while LOGGED_IN, code == 4000** (superseded):
   - Transition LOGGED_IN → INITIAL. Clear cached name.
   - Print `disconnected: superseded by another session`.
   - **Do NOT auto-reconnect** (the name is no longer ours).

**`/send <recipient> <text>` while OFFLINE (transparent queueing):**

The user just types — the client routes based on current state. While
OFFLINE:

1. Validate as in the LOGGED_IN case (recipient regex; non-empty text).
2. Generate a UUID v4 (`id`).
3. Append one row to the outbox file (see "Outbox file" below):
   `{"id": "...", "to": "...", "text": "...", "status": "pending"}`
4. Print `queued`. Do NOT attempt to transmit (we're offline; the
   reconnect loop will flush this row once it succeeds — Iteration 5).

## Outbox file

Each client keeps a **per-user outbox file** at
`./outbox-<name>.jsonl` (relative to the client's current working
directory). JSON Lines: one object per line, UTF-8, LF newlines.

**Row schema (strict — exactly these four keys):**

```jsonc
{
  "id":     "<UUID v4>",
  "to":     "<recipient name>",
  "text":   "<message text>",
  "status": "pending"     // or "sent" once acked by the server
}
```

**Lifecycle:**

- On `/send` while OFFLINE (i.e., the WebSocket is currently down):
  append a new row with `status: "pending"`. Lazy file creation.
- On `/send` while LOGGED_IN: **do NOT write to the outbox.** Live
  sends are not persisted (outbox holds offline-queued messages only).
- The file is never truncated automatically.

## Outbox flush (on every LOGGED_IN entry)

The outbox flushes whenever the client transitions to LOGGED_IN —
both fresh `/login` (covers the relaunched-process case) and
auto-reconnect (covers the in-process recovery case). Same procedure
either way:

1. Read the outbox file (top to bottom, in file order).
2. For each row with `status: "pending"`:
   a. Send `{"type": "send", "id": id, "to": to, "text": text}`.
   b. Wait for `send_ok` with matching `id`.
   c. Update the row's `status` to `"sent"`.
   d. Rewrite the file (atomic: write to `outbox-<name>.jsonl.tmp`,
      then rename over the original).
3. Skip rows that already have `status: "sent"`.

**Mid-flush disconnect.** If the WebSocket dies before a `send_ok`
arrives, the current row stays `pending`. The next reconnect retries
it. (Iteration 8 will add UUID-based server dedup so retries are safe
at-most-once.)

**Recipient state during flush.** If the recipient is online, they
receive `deliver` per §"Server behavior". If they're offline, the
message is silently dropped (Iteration 6 will add the server inbox).

**Stdout discipline.** Every status line is flushed immediately
(line-buffered or explicit per-line flush). One status line per
state change. No interactive prompt characters.

## Errors

| Trigger | Stdout (exact) |
|---|---|
| `/login` with no name | `error: usage: /login <name>` |
| `/login <invalid>` (regex fails) | `error: usage: /login <name>` |
| `/login` while LOGGED_IN | `error: already logged in as <current>` |
| Server unreachable | `error: cannot reach server` |
| Server closes connection with code 4000 | `disconnected: superseded by another session` |
| `/send` with no args | `error: usage: /send <recipient> <text>` |
| `/send <recipient>` (no text) | `error: usage: /send <recipient> <text>` |
| `/send <invalid-recipient> <text>` | `error: usage: /send <recipient> <text>` |
| `/send` while INITIAL | `error: not logged in; use /login <name> first` |

## Concurrency and ordering

**Runtime model.** Both server and client are **single-threaded event
loops** (Python `asyncio`; Node async/await). One event at a time: a
CLI command, an inbound frame, or a file I/O. No worker threads.

**Per-sender FIFO at the server.** If alice sends `m1` then `m2` to
bob, the server enqueues them into `inbox[bob]` in that order and
pushes them to bob (online or on next login) in that order.

**Per-sender FIFO at the client outbox.** Rows are appended in the
order of `/send` commands. Outbox flush replays them in file order,
serially (one `send_ok` await before the next frame).

**Cross-sender ordering.** No guarantee. If alice and carol both send
to offline bob, bob may receive them in either order on next login.

**Outbound flush vs inbound push (the reconnect interleave).** On
reconnect, the client's outbox flush and the server's inbox push run
on the same WebSocket. Frames interleave in arrival order — no causal
relationship between alice's outgoing `send` frames and the `deliver`
frames the server pushes to her. The conformance test exercises this
in Step 7 (see below).

**login_ok ordering.** The server sends `login_ok` strictly before
any `deliver` frame in the same session. This is the only ordering
rule the protocol guarantees about session start.

## Protocol errors

**Frame size cap.** Inbound frames larger than **16 KiB (16384 bytes)**
are rejected. Server closes the connection with code **4001**, reason
`oversize frame`.

**Server-side strictness.** The server validates each inbound frame.
On any violation it closes the connection with WebSocket close code
**4001** and one of the following exact reason strings:

| Violation | Close reason |
|---|---|
| Frame body is not valid JSON | `invalid json` |
| Valid JSON but not a top-level object | `invalid frame` |
| Missing or non-string `type` field | `invalid frame` |
| Unknown `type` value | `invalid frame` |
| Wrong key set for the frame type (missing or extra keys) | `invalid frame` |
| Frame size exceeds 16 KiB | `oversize frame` |
| `send` from a connection that hasn't `login`'d yet | `send: not logged in` |
| Second `login` on a connection already logged in | `already logged in on this connection` |

The reason strings are deterministic and conformance-test-checkable.

**Client-side strictness.** Clients SHOULD parse inbound frames
defensively; malformed frames from the server are a protocol error
and the client SHOULD close the connection. For v1 the clients
tolerate malformed inbound by dropping the frame silently (the
reference server never sends malformed frames). Tightening this is
deferred — see FUTURE.md.

## Conformance scenario

The conformance test harness drives each case below and asserts the
expected stdout. All cases must pass for the current scope to be green.

### Step 1.a — happy path login

- Action: type `/login alice` into client.
- Expected stdout: `logged in as alice`

### Step 1.b — invalid name rejected

- Action: type `/login bad@name`.
- Expected stdout: `error: usage: /login <name>`

### Step 1.c — server unreachable

- Pre: server is not running.
- Action: type `/login alice`.
- Expected stdout: `error: cannot reach server`

### Step 1.d — duplicate `/login` while LOGGED_IN

- Pre: alice is LOGGED_IN.
- Action: type `/login bob`.
- Expected stdout: `error: already logged in as alice`

### Step 1.e — supersede

- Pre: alice is LOGGED_IN on client A.
- Action: open client B in the same name space, type `/login alice`.
- Expected stdout (client A): `disconnected: superseded by another session`
- Expected stdout (client B): `logged in as alice`

### Step 2.a — happy path online send

- Pre: alice and bob both LOGGED_IN.
- Action (alice): `/send bob hi bob, what's up?`
- Expected stdout (bob): `[alice → bob]  hi bob, what's up?`

### Step 2.b — /send with no args

- Pre: alice LOGGED_IN.
- Action: `/send`
- Expected stdout: `error: usage: /send <recipient> <text>`

### Step 2.c — /send with no text

- Pre: alice LOGGED_IN.
- Action: `/send bob`
- Expected stdout: `error: usage: /send <recipient> <text>`

### Step 2.d — /send to self

- Pre: alice LOGGED_IN.
- Action: `/send alice hello self`
- Expected stdout (alice): `[alice → alice]  hello self`

### Step 2.e — /send while INITIAL

- Pre: client just started (INITIAL).
- Action: `/send bob hi`
- Expected stdout: `error: not logged in; use /login <name> first`

### Step 3.a — server failure triggers OFFLINE automatically

- Pre: alice LOGGED_IN.
- Action: stop the server (kill its process).
- Expected stdout (alice): `disconnected` (printed once, on transition).

### Step 3.b — /send while OFFLINE queues (Iteration 4 will add the outbox)

- Pre: alice OFFLINE (server stopped).
- Action: `/send bob hi`
- Expected stdout: `queued`

### Step 3.c — server restoration auto-reconnects

- Pre: alice OFFLINE (reconnect loop is running).
- Action: restart the server.
- Expected stdout (alice): `reconnected` (printed once, on transition back to LOGGED_IN).

### Step 3.d — supersede after reconnect halts the reconnect loop

- Pre: alice LOGGED_IN. Stop the server (alice goes OFFLINE, reconnect
  loop running). Restart the server (alice auto-reconnects to LOGGED_IN
  and prints `reconnected`).
- Action: open a second client and type `/login alice` there.
- Expected stdout (alice): `disconnected: superseded by another session`.
  Alice transitions to INITIAL; **no subsequent `reconnected` line**
  appears even though the server is still up. This is the invariant
  unique to the new model — close-code 4000 stops auto-reconnect.

### Step 4.a — outbox row created on first OFFLINE /send

- Pre: alice OFFLINE.
- Action: `/send bob hi bob`
- Expected stdout: `queued`
- Expected disk state: `outbox-alice.jsonl` exists in cwd, contains one row:
  `{"id":"<uuid>","to":"bob","text":"hi bob","status":"pending"}` followed by `\n`.

### Step 4.b — multiple OFFLINE sends append in order

- Pre: alice OFFLINE.
- Action: `/send bob first`, then `/send carol second`, then `/send bob third`.
- Expected stdout: `queued` three times.
- Expected disk state: `outbox-alice.jsonl` contains exactly three rows
  in that order, each `status=pending`.

### Step 4.c — online /send still does NOT write to outbox

- Pre: alice LOGGED_IN, bob LOGGED_IN.
- Action (alice): `/send bob hi`
- Expected: bob receives the message (per Step 2.a). `outbox-alice.jsonl`
  is empty / non-existent (live sends are NOT persisted in Iteration 4).

### Step 5.a — flush delivers queued messages on auto-reconnect

- Pre: alice LOGGED_IN, bob LOGGED_IN. Stop the server (alice and bob
  both transition to OFFLINE and print `disconnected`).
- Action (alice): `/send bob first`, `/send bob second` (both queue
  to `outbox-alice.jsonl`).
  Then restart the server (alice and bob auto-reconnect).
- Expected stdout (alice): `disconnected`, `queued`, `queued`, `reconnected`.
- Expected stdout (bob): `disconnected`, `reconnected`, then
  `[alice → bob]  first` THEN `[alice → bob]  second` (in send order).
- Expected disk state: `outbox-alice.jsonl` contains 2 rows, **both
  `status: "sent"`**.

### Step 5.b — flush with already-sent rows skips them

- Pre: alice has an outbox with 1 row already `status: "sent"` (from a
  prior session) and 0 pending rows. Server cycle (stop + restart)
  triggers a fresh flush.
- Action: stop server, restart server.
- Expected: no new wire traffic for the already-`sent` row. File
  unchanged.

### Step 6.a — server holds message for offline recipient

- Pre: alice LOGGED_IN. bob has never logged in (or is OFFLINE).
- Action (alice): `/send bob hi`
- Expected: alice gets `send_ok`. Bob doesn't see the message yet.
- Then: bob `/login bob`.
- Expected stdout (bob): `logged in as bob`, then `[alice → bob]  hi`.

### Step 6.b — multiple queued messages flushed in FIFO order on login

- Pre: alice LOGGED_IN. bob offline. alice sends 3 messages to bob.
- Action: bob `/login bob`.
- Expected stdout (bob): three `[alice → bob]  …` lines in send order.

### Step 6.c — deliver_ok clears the inbox (no re-delivery on reconnect)

- Pre: alice sent 1 message to offline bob; bob logged in and saw it.
- Action: cycle the server (stop, then restart). Bob's WS drops, bob
  auto-reconnects and re-logs in.
- Expected: bob does NOT receive the message again. (`deliver_ok` from
  the first delivery removed it from the inbox.)

### Step 7 — the brief's full 7-step scenario (concurrent reconnect)

The integration test. The brief's exact 7-step scenario must pass
end-to-end against any conformant pair of clients. This is the
strongest signal that the protocol is correct.

Per-client network drops below are simulated by the test harness;
the spec is mechanism-agnostic (closing the underlying socket and
killing+relaunching the client process are both valid). What the
client must do on disconnect and reconnect is fixed by the state
machine and the outbox lifecycle.

1. Alice → Bob: `"Hi Bob, I have something important to tell you"` (live)
2. Bob → Alice: `"What is it?"` (live)
3. Alice's network drops. She queues `/send bob "the something"`
   (queues locally to `outbox-alice.jsonl`).
4. Bob's network drops. He queues `/send alice "anything"` (queues
   locally to `outbox-bob.jsonl`).
5. Alice's network restores — she auto-reconnects and her outbox
   flushes to the server. The server queues `"the something"` in
   `inbox[bob]` since bob is still offline.
6. Alice's network drops again (she never sees bob's eventual reply
   in this run).
7. Bob's network restores — he auto-reconnects. Concurrently:
   - The server flushes `inbox[bob]` to bob's connection (he sees
     `"the something"`).
   - Bob's outbox flush sends `"anything"` to the server; the server
     queues it in `inbox[alice]` since alice is now offline.

Expected end state:
- Bob's terminal: `[alice → bob]  Hi Bob, I have something important to tell you`,
  `[alice → bob]  the something` (plus the live send/recv lines from steps 1-2).
- Alice's terminal: `[bob → alice]  What is it?` (from step 2 only —
  she's offline when bob's queued message gets delivered to the server).
- Alice's outbox: 1 row, `status=sent` (`"the something"`).
- Bob's outbox: 1 row, `status=sent` (`"anything"`).
- Server `inbox[alice]`: contains `"anything"` (un-delivered, awaits
  alice's next reconnect).
- Server `inbox[bob]`: empty (delivered + acked).

### Step 8.a — retry with same id deduplicates

- Pre: alice and bob both LOGGED_IN.
- Action: send TWO `send` frames over the wire with the same UUID
  (e.g., via a raw WebSocket — simulates a client retry after a
  disconnect that lost the original `send_ok`).
- Expected: both sends receive `send_ok`. **Bob receives exactly ONE
  `deliver` frame** (the first; the second is silently dropped).

### Step 8.b — retry with same id but different content is still deduped

- Pre: alice and bob both LOGGED_IN; carol is a third online user.
- Action: send `{id=X, to=bob, text="first"}`, then
  `{id=X, to=carol, text="different"}`. Dedup is on `(sender, id)`
  alone — the second is treated as a duplicate.
- Expected: bob receives "first". Carol receives nothing (the second
  frame is dropped entirely). Both sends get `send_ok`.

### Step 9.a — malformed JSON closes with `invalid json`

- Action: send raw text `"{not json"` (or any non-parseable body) to
  the server over an open WebSocket.
- Expected: server closes connection. WS close code 4001, reason
  `invalid json`.

### Step 9.b — unknown frame type closes with `invalid frame`

- Action: send `{"type": "nope", "name": "alice"}`.
- Expected: WS close code 4001, reason `invalid frame`.

### Step 9.c — extra fields closes with `invalid frame`

- Action: send `{"type": "login", "name": "alice", "extra": "field"}`.
- Expected: WS close code 4001, reason `invalid frame`.

### Step 9.d — oversize frame closes with `oversize frame`

- Action: send a frame whose UTF-8 body exceeds 16 KiB.
- Expected: WS close code 4001, reason `oversize frame`.

### Step 10.a — `/help` prints the command list

- Pre: any state.
- Action: type `/help`.
- Expected stdout: the 4-line help block listing `/login`, `/send`,
  `/help`, `/quit`. (No `/offline` or `/online` — disconnect and
  reconnect are network-driven, not commands.)

### Step 10.b — `--version` prints `messaging-client 1.0.0`

- Action: run the client as `python clients/python/client.py --version`
  (or `node clients/typescript/dist/client.js --version`).
- Expected stdout: `messaging-client 1.0.0`. Process exits 0.

### Step 10.c — unknown command prints help-pointing error

- Pre: any state.
- Action: type `/bogus` or any unknown verb.
- Expected stdout: `error: unknown command — try /help`
