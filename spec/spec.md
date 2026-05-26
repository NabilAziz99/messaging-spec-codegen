# Messaging app — behavior spec

This is the **source of truth**. Two independent client implementations
in different languages, generated against this spec, must
interoperate. Where the generated code disagrees with the spec, the
spec wins.

This spec is built iteratively. **Iteration 1 (login) complete.**

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

Examples:

```json
// C→S: login
{"type": "login", "name": "alice"}

// S→C: login_ok
{"type": "login_ok", "name": "alice"}
```

**Name validation.** `^[A-Za-z0-9_-]{1,32}$` — ASCII letters, digits,
underscore, hyphen. Length 1-32. Case-sensitive.

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

## Client behavior

**State machine.** Two states:

```
                /login <name>
   INITIAL ──────────────────► LOGGED_IN
```

**Commands accepted in INITIAL:** `/login`, `/help`, `/quit`.
**Commands accepted in LOGGED_IN:** `/help`, `/quit`.

**`/login <name>` behavior:**

1. Validate `name` locally. If invalid → print `error: usage: /login <name>`, stay INITIAL.
2. If already LOGGED_IN → print `error: already logged in as <current_name>`, do nothing.
3. Open a WebSocket to the server. If connection fails → print `error: cannot reach server`, stay INITIAL.
4. Send `{"type": "login", "name": <name>}`.
5. Wait for `login_ok`.
6. Transition INITIAL → LOGGED_IN. Print `logged in as <name>`.

## Errors

| Trigger | Stdout (exact) |
|---|---|
| `/login` with no name | `error: usage: /login <name>` |
| `/login <invalid>` (regex fails) | `error: usage: /login <name>` |
| `/login` while LOGGED_IN | `error: already logged in as <current>` |
| Server unreachable | `error: cannot reach server` |
| Server closes connection with code 4000 | `disconnected: superseded by another session` |

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
