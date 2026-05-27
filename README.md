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

32 conformance cases — both clients must pass all of them. The
critical network gates (server-stop → OFFLINE, supersede halts
reconnect, outbox flush on every LOGGED_IN entry, deliver_ok clears
the inbox, brief's 7-step scenario) live in `DESIGN.md` § "Testing
unreliable networks".

## Regenerate the clients

```bash
rm -rf clients/python clients/typescript
claude
> /regen-clients
```

## Layout — who wrote what

| Path | Author | What that means |
|---|---|---|
| `spec/spec.md` | me | Sections: Wire protocol (6 frames + their exact key sets + validation rules), Server behavior, Client behavior (3-state machine, `/login`, `/send`, implicit network transitions), Outbox file format, Outbox flush procedure, Errors, Concurrency and ordering, Protocol errors (close codes + reason strings), Conformance scenario (Steps 1.a through 10.c). |
| `spec/README.md` | me | The 10-iteration roadmap: (1) login + name validation, (2) online send + receive, (3) implicit offline state, (4) outbox queue, (5) outbox flush on auto-reconnect, (6) server inbox for offline recipients, (7) brief's full 7-step scenario, (8) UUID dedup, (9) protocol errors, (10) polish. |
| `DESIGN.md`, `FUTURE.md` | me | Every design choice and the trade-off it links to in `FUTURE.md`. |
| `.claude/commands/regen-clients.md` | me | The `/regen-clients` orchestrator: preconditions, phase 1 (spawn Python + TypeScript generators in parallel), phase 2 (conformance check on both clients), phase 3 (report). |
| `.claude/agents/python-client-generator.md`, `typescript-client-generator.md`, `conformance-checker.md` | me | Three scope-restricted sub-agents (each with its own tool list and workspace it can't escape). Every "Critical behavior" pin in the two generator recipes came from a real conformance failure — added explicitly so future regens don't reproduce it. |
| `server/protocol.py`, `server/handlers.py`, `server/server.py` | mostly me | I designed: the WebSocket endpoint (`ws://127.0.0.1:8765/ws`); the 6 frame types and their exact key sets (`login {type,name}`, `login_ok {type,name}`, `send {type,id,to,text}`, `send_ok {type,id}`, `deliver {type,id,from,to,text,server_ts}`, `deliver_ok {type,id}`); close codes (4000 supersede, 4001 protocol error); the 16 KiB frame-size cap; `EXPECTED_KEYS` set-equality validation (extras rejected, not silently dropped); the `(sender, id)` dedup key; the inbox lifecycle (append on `send` → push if recipient online → keep until `deliver_ok` removes it); the 3-module split (`protocol` = pure validation, `handlers` = state + per-frame logic, `server` = WS lifecycle). Claude wrote the Python under each decision. |
| `test/framework.py`, `test/runner.py` | mostly me | Harness primitives I designed: `ServerHandle.stop()/.start()` (bounce the server mid-test — global outage), `ClientHandle.kill()` + `restart_client(prev, cmd)` (SIGKILL one client and respawn in the same cwd — per-client outage with on-disk outbox recovery), `wait_for(expected)` (cursor-advancing so repeated calls find each NEW occurrence), `assert_not_seen(forbidden, window)` (forward-looking — baseline + sleep + scan only new lines). Claude wrote the Python. |
| `test/cases/case_03_offline.py`, `case_05_outbox_flush.py`, `case_07_reconnect.py`, `case_08_dedup.py` | mostly me | The correctness gates. Network behaviors: **Step 3.a** (`server.stop()` → `wait_for("disconnected")` — catches the Python `async for raw in ws` silent-exit bug now pinned in the Python recipe). **Step 3.d** is the strongest invariant — `assert_not_seen("reconnected", 1.5)` after a forced 4000 proves the reconnect timer is actually cancelled, not just dormant. **Step 5.a + Step 7** prove the outbox flushes on every LOGGED_IN entry (auto-reconnect path + relaunched-process path via `restart_client()`). **Step 6.c** proves `deliver_ok` clears the server inbox (no re-delivery on reconnect). **Step 7** is the brief's full hybrid scenario (server cycle + `kill()` + `restart_client()`). **Step 8** (`case_08`): the `(sender, id)` dedup — same id with different `to` or `text` is still a duplicate (first-write-wins), defending against an adversarial client recycling an id. |
| `test/cases/case_01_login.py`, `case_02_send.py`, `case_04_outbox_queue.py`, `case_06_server_inbox.py`, `case_09_protocol_errors.py`, `case_10_polish.py` | mostly Claude | Mechanical cases against an already-clear spec. Claude wrote them; I reviewed for spec adherence. |
| `chat.py` | Claude (fully AI-generated) | 2-party demo wrapper around a spec-compliant client. Auto-logs in, banner, plain-text → `/send` translation, `--py`/`--ts` flag. Not part of the regeneration contract. |
| `clients/python/`, `clients/typescript/` | Claude (fully AI-generated) | Produced by `/regen-clients` from `spec/spec.md` + the recipes in `.claude/`. I do **not** hand-edit them. When a regen produces a buggy client, I update the spec or the recipe and regenerate — never the client directly. |

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

The Layout table above maps every path to its author and the
specific decisions inside it. For the full reasoning and trade-offs
behind each decision, see `DESIGN.md`.

### Workflow this implies

Failures during the build always traced back to one of three places:
spec, recipe, or implementation. When a conformance case failed I'd
diagnose which layer was wrong and fix it there. Two examples:

1. Outbox initially held every `/send` (live + offline). Conformance
   failed Step 4.c. Root cause: spec was ambiguous about live-send
   persistence. Fix: tightened the spec, regenerated.
2. A Python regen didn't ack `deliver` frames; server re-delivered on
   every login. Root cause: the spec said it, the generator missed it.
   Fix: pinned the requirement in the Python recipe item #1.

In both cases the AI produced something; I judged it against the
conformance test, located the actual fix (spec vs. recipe vs. code),
and updated the right artifact — never patching the generated client
directly.

For the system-design reasoning see `DESIGN.md`. For what's
deliberately deferred see `FUTURE.md`.
