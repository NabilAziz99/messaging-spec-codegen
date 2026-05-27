# Messaging app

Two CLI clients (Python and TypeScript) and a single server. Everything
runs on localhost over WebSocket.

## What it does

- A user identifies themselves by name on first launch (`/login alice`).
- They send text to another user (`/send bob hi`).
- If the sender is offline (the WebSocket is down), the message queues
  to a file on disk and flushes the next time the network is back.
  Going offline is **not** a command — the client detects it from the
  socket dying and auto-reconnects in the background with backoff.
- If the recipient is offline, the server holds the message and pushes
  it when they next log in.
- Retries are safe — every message has a UUID, the server dedups, so
  reconnect-and-replay doesn't double-deliver.

Six JSON frame types over one WebSocket per online client. See
`spec/spec.md` for the full contract.

## Run it

```bash
pip install -r requirements.txt
```

Then open three terminals. The friendliest way to chat is through
`chat.py`, which prompts you for your name and the recipient's name,
auto-logs you in, prints a banner so it's obvious what to do, and
lets you just type messages (no `/send <name>` for every line):

```bash
# Terminal 1 — server
python server/server.py

# Terminal 2 — one peer on Python
python chat.py
Your name? alice
Who do you want to chat with? bob

# Terminal 3 — other peer on TypeScript
python chat.py --ts
Your name? bob
Who do you want to chat with? alice
```

`chat.py` also accepts both names as positional args if you'd rather
skip the prompts: `python chat.py alice bob --ts`.

Use `--py` (the default) or `--ts` to pick which generated client to
wrap. You can mix-and-match — one peer on each language is the most
interesting demo, since it proves cross-language interop end-to-end.

You'll see a banner like:

```
─────────────────────────────────────────────────────────────
  Connected as: alice    →    Talking to: bob
─────────────────────────────────────────────────────────────
  Type a message and hit Enter.
  Slash-commands work too:  /help  /quit
─────────────────────────────────────────────────────────────
logged in as alice
alice → bob > hi bob, what's up?
[alice → bob]  hi bob, what's up?
alice → bob > [bob → alice]  loud and clear
alice → bob >
```

### Or use the spec-compliant CLI directly

`chat.py` is a thin rendering layer; the spec-compliant clients
underneath speak `/send <recipient> <text>` for every message. If you
want to drive them directly (this is what the test runner does):

```bash
python clients/python/client.py
> /login alice
> /send bob hello

node clients/typescript/dist/client.js
> /login bob
```

No banner — the raw clients implement only what the spec mandates.

## Test it

```bash
python test/runner.py --client-cmd "python clients/python/client.py"
python test/runner.py --client-cmd "node clients/typescript/dist/client.js"
```

32 conformance cases. Both clients must pass all of them.

### How the critical network behaviors were evaluated

The implicit-network model has several places where a half-correct
implementation would silently pass without an explicit test. Each
gets a dedicated one:

| Behavior | Test | How |
|---|---|---|
| **WS-drop → OFFLINE** | Step 3.a | `server.stop()` mid-test, then `alice.wait_for("disconnected")`. Catches the Python `async for raw in ws` silent-exit bug (we hit this; pinned in the generator recipe). |
| **Supersede halts the reconnect loop** *(the correctness gate)* | Step 3.d | After a successful auto-reconnect, kick alice from a second client; then `alice.assert_not_seen("reconnected", window=1.5)`. Catches a retry timer that keeps ticking forever after close-code 4000. |
| **Outbox flush on every LOGGED_IN entry** | Step 5.a + Step 7 | 5.a exercises the auto-reconnect path; Step 7 exercises the fresh-`/login` path via `restart_client()` — proves a killed-and-relaunched process recovers pending sends from disk. |
| **`deliver_ok` clears the server inbox** | Step 6.c | bob receives, server cycle, bob auto-reconnects; `bob.assert_not_seen(msg, 0.4)`. Catches the server re-delivering acked messages on every reconnect. |
| **Brief's full 7-step scenario** | Step 7 | Hybrid: `server.stop()/.start()` for the global-outage phase + `ClientHandle.kill()` + `restart_client()` for the asymmetric phase (alice and bob independently offline). Asserts the full final state on both terminals, both outboxes, and both server inboxes. |

Two harness primitives unlock all of this:
`ServerHandle.stop()/.start()` (global outage — every WS dies at
once) and `ClientHandle.kill()` + `restart_client(prev, cmd)`
(per-client outage with the on-disk outbox surviving). See
`DESIGN.md` § "Testing unreliable networks" for the rationale and
the rejected alternative.

## Regenerate the clients

```bash
rm -rf clients/python clients/typescript
claude
> /regen-clients
```

## Layout

| Path | Authored |
|---|---|
| `spec/spec.md` | hand-written (the contract) |
| `.claude/` | hand-written (the generator config) |
| `server/`, `test/` | mixed (hand-written + AI-assisted) |
| `chat.py` | mixed (hand-written + AI-assisted) — demo wrapper for 2-party chat UX |
| `clients/python/`, `clients/typescript/` | **AI-generated** by `/regen-clients` |

## What the Claude prompts do

The `.claude/` directory holds one slash command and three focused
sub-agents. When you run `/regen-clients`, the orchestrator dispatches
them in order:

- **`python-client-generator`** — reads `spec/spec.md`, writes the
  four Python client modules (`protocol`, `outbox`, `connection`,
  `client`), pinned to ~150-200 lines each. Sanity-checks with
  `--help` before returning.
- **`typescript-client-generator`** — same for TypeScript. Compiles
  to `dist/` with `tsc`.
- **`conformance-checker`** — runs `test/runner.py` against the
  freshly-generated clients and reports PASS or FAIL.

The orchestrator never writes code itself — only delegates. Each
sub-agent has a restricted tool list and scoped workspace (the
Python generator can't touch the TypeScript directory and vice versa).

## How this was built

We started by separating concerns: server, clients, spec, test
harness, and the Claude prompts that would generate the clients.
Each piece had a clear responsibility before any code got written.
The first real artifact was the [spec roadmap](spec/README.md) — ten
focused features (login, send, offline, outbox, reconnect, server
inbox, dedup, protocol errors, polish), each with its own conformance
cases. From there it was hand-writing-with-AI-assist for the
foundation (server, test framework), then capturing the
client-generation logic in `.claude/agents/` and `.claude/commands/`,
then running `/regen-clients` to produce the clients themselves.

Two corrections worth flagging mid-build. The outbox initially held
every `/send`, but the spec actually wanted offline-only — caught
when a conformance case failed. And a separate run of
`/regen-clients` produced a Python client that didn't ack `deliver`
frames, so the server kept re-delivering on subsequent logins — the
recipe didn't lift that requirement out of the spec, fixed by pinning
it explicitly in the prompt. Both surfaced from running the
conformance test in fresh sessions and tightening the prompts in
response.

For the system-design reasoning see `DESIGN.md`. For what's
deliberately deferred see `FUTURE.md`.
