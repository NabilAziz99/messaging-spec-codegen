# Messaging app — behavior spec

This is the **source of truth**. Two independent client implementations
in different languages, generated against this spec, must
interoperate. Where the generated code disagrees with the spec, the
spec wins.

This spec is built iteratively. **Iterations 1-3 complete (login + online send + implicit offline state).**

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

**On `login` frame:**

1. Validate `name` against the name pattern. If invalid, close with
   WebSocket close code 4001.
2. If `connections[name]` already exists, close that older connection
   with code 4000 reason `"superseded"`.
3. Register: `connections[name] = ws`.
4. Send `{"type": "login_ok", "name": name}` to the new connection.

**On disconnect.** Remove `connections[name]` if it points to this
socket.

**On `send` frame:**

1. Validate fields: `id` is a UUID v4 string; `to` matches the name pattern;
   `text` is a string.
2. Determine sender from the connection's registered user. If the
   connection isn't logged in, close with 4001.
3. Set `server_ts = int(time.time())`.
4. If `connections[to]` exists, push a `deliver` frame to that
   connection immediately. (If the recipient is offline, the message
   is silently dropped — Iteration 6 will add the server inbox.)
5. Send `{"type": "send_ok", "id": id}` to the sender.

**On `deliver_ok` frame:**

No-op for now. The frame is part of the protocol so clients can
acknowledge delivery, but with no inbox yet (Iteration 6) there's
nothing to clean up.

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
     - On `login_ok` → transition OFFLINE → LOGGED_IN. Print `reconnected`.
     - On `login_ok` failure or close 4000 during reconnect → transition to INITIAL (cached name lost). Print `disconnected: superseded by another session`.

2. **On WebSocket close while LOGGED_IN, code == 4000** (superseded):
   - Transition LOGGED_IN → INITIAL. Clear cached name.
   - Print `disconnected: superseded by another session`.
   - **Do NOT auto-reconnect** (the name is no longer ours).

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
