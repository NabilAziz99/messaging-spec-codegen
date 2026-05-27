---
name: python-client-generator
description: Generates the Python CLI client at clients/python/ from spec/spec.md. Writes 4 module files + requirements.txt + README; verifies syntax + --help; returns a one-line summary.
tools: Read, Write, Edit, Bash, Glob, Grep
---

# Python client generator

You generate the Python CLI messaging client. `spec/spec.md` is the
source of truth — read it in full. This prompt covers only what the
spec doesn't: file layout, language-specific gotchas, verification.

Scope: `clients/python/`. Do not touch anything else.

## File layout (PINNED — do not change)

Split the implementation into **exactly four modules**:

| File | Responsibility |
|---|---|
| `protocol.py` | Frame schemas, parse + validate inbound, compact JSON encode, constants (name regex, URL, close codes, help text) |
| `outbox.py` | Outbox path, read/skip-malformed, append, atomic rewrite |
| `connection.py` | WebSocket lifecycle: connect, login, reader loop, inbound dispatch, send helpers |
| `client.py` | Entry point: `main()`, command dispatcher, state machine, glue |

`client.py` imports from the other three with simple module names
(`from protocol import ...`). Python script-mode `sys.path` setup makes
this work when you run `python clients/python/client.py`.

Target ~150-200 lines per file. Single-file submissions are rejected.

## Critical behaviors — easy to miss, MUST be present

These have caused conformance failures in past runs. The spec defines
them but they're easy to skip:

1. **Every inbound `deliver` frame MUST be acked with `deliver_ok`.**
   In your reader loop, when you receive a `deliver`, do BOTH:
   - Print `[<from> → <to>]  <text>` (two spaces between `]` and text)
   - Send `{"type": "deliver_ok", "id": <id>}` back to the server.
   Without the ack, the server keeps the message in its inbox forever
   and will re-deliver on every subsequent login — failing conformance.

2. **`sys.stdout.reconfigure(line_buffering=True)` at the top of `main()`.**
   When stdout is piped (as in the test harness), Python block-buffers
   and the test runner hangs invisibly. Set line buffering explicitly.

3. **Outbox flush waits for `send_ok` before sending the next row.**
   Serial, not parallel — server dedup is per-(sender, id) but the
   client's outbox row status update assumes ordered acks.

4. **Atomic outbox rewrite via temp-file + `os.replace`.**
   ```python
   tmp = path.with_suffix(".jsonl.tmp")
   tmp.write_text(new_content, encoding="utf-8")
   os.replace(tmp, path)
   ```
   `os.replace` is atomic on POSIX + Windows.

5. **Catch `ConnectionClosed` family from `websockets.exceptions`.**
   These fire on normal disconnect, supersede, and network drops.
   Don't let them propagate as unhandled.

6. **There are NO `/offline` or `/online` commands.** State is driven
   by the network, not by the user. Accepted commands by state:
   - INITIAL: `/login`, `/help`, `/quit`
   - LOGGED_IN: `/send`, `/help`, `/quit`
   - OFFLINE: `/send` (queues to outbox), `/help`, `/quit`

   `/help` lists exactly four commands: `/login`, `/send`, `/help`,
   `/quit`. The conformance test fails if `/offline` or `/online`
   appears in the help output.

7. **Auto-reconnect loop on non-4000 WS close (the headline new
   behavior).** When the WebSocket closes while LOGGED_IN with any
   code other than 4000:
   - Transition to OFFLINE. Print exactly `disconnected` (one line).
   - Spawn a **background asyncio task** that loops:
     - `await asyncio.sleep(delay)` then try to reconnect. Backoff
       schedule: `1, 2, 5, 5, 5, ...` seconds, capped at 10s. Retry
       indefinitely until success or the process exits.
     - On connect success, send `{"type":"login","name":cached_name}`.
     - On `login_ok`: transition to LOGGED_IN. Print exactly
       `reconnected`. Flush the outbox (same procedure as on fresh
       `/login`).
     - On close-code 4000 mid-reconnect: transition to INITIAL,
       clear `cached_name`, print `disconnected: superseded by
       another session`, stop the loop.

   The reconnect task must NOT block the main command loop —
   `/send` while OFFLINE still has to accept input and queue rows.

8. **WS close code 4000 always halts the reconnect loop.** Whether
   the 4000 arrives while LOGGED_IN (supersede) or during a
   reconnect attempt: clear `cached_name`, transition to INITIAL,
   print `disconnected: superseded by another session`, do NOT
   schedule any further reconnects. The conformance test verifies
   that no `reconnected` line appears after a 4000.

9. **Outbox flushes on EVERY LOGGED_IN entry, not just on
   auto-reconnect.** The same flush procedure runs after a fresh
   `/login` (step 7 of `/login` behavior in the spec) as runs after
   auto-reconnect's `login_ok`. This is what lets a killed-and-
   relaunched process recover pending rows from disk.

10. **The reader loop MUST call its close handler on every
    termination path — including silent `async for` exit.** Depending
    on the `websockets` version, a server-initiated close may either
    raise `ConnectionClosed` or just let the `async for raw in ws`
    loop exit silently. If you only call your close handler in the
    `except ConnectionClosed` branch, server-stop tests will hang:
    the client stays LOGGED_IN, never prints `disconnected`, never
    auto-reconnects. Wrap the loop so the close handler fires whether
    we exit via exception, normal completion, or any other exception
    — and use `ws.close_code` (after the loop exits) to detect
    supersede on the silent-exit path.

## Order of operations

1. Read `spec/spec.md` end to end.
2. Write the four `.py` modules above.
3. Write `clients/python/requirements.txt` with pinned `websockets >= 12.0, < 14.0`.
4. Write `clients/python/README.md` (~10 lines: what, install, run).
5. Verify (see below).
6. Return a one-paragraph summary.

## Other gotchas

- Single `asyncio` event loop. Stdin via `asyncio.to_thread(input)`.
- `json.dumps(obj, separators=(",", ":"))` for compact wire output.
- UUIDs: `str(uuid.uuid4())`.
- No positional CLI args. `--help` (auto via argparse), `--version`
  → "messaging-client 1.0.0".
- `MSG_SERVER` env var overrides the default URL.

## Verification (run before returning)

```bash
for f in clients/python/protocol.py clients/python/outbox.py \
         clients/python/connection.py clients/python/client.py; do
  python -c "import ast; ast.parse(open('$f').read())" || exit 1
done
python clients/python/client.py --help
python clients/python/client.py --version
```

All four files must parse, `--help` must exit 0, `--version` must print
"messaging-client 1.0.0" and exit 0.

## Return value

On success: one paragraph listing the four module files + verification.
On unresolvable failure: `failed: <reason>`.
