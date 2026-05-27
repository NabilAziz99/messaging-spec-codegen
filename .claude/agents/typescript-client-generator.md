---
name: typescript-client-generator
description: Generates the TypeScript CLI client at clients/typescript/ from spec/spec.md. Writes 4 .ts modules + package.json + tsconfig.json + README; runs npm install + tsc + --help; returns a one-line summary.
tools: Read, Write, Edit, Bash, Glob, Grep
---

# TypeScript client generator

You generate the TypeScript CLI messaging client. `spec/spec.md` is
the source of truth — read it in full. This prompt covers only what
the spec doesn't: file layout, language-specific gotchas, pinned configs.

Scope: `clients/typescript/`. Do not touch anything else.

## File layout (PINNED — do not change)

Split the implementation into **exactly four modules**:

| File | Responsibility |
|---|---|
| `protocol.ts` | Frame discriminated-union types, parse + validate inbound, constants |
| `outbox.ts` | Outbox path, read/skip-malformed, append, atomic rewrite |
| `connection.ts` | WebSocket lifecycle: connect, login, message dispatch, send helpers |
| `client.ts` | Entry point: `main()`, command loop, state machine, glue |

`client.ts` imports from the other three. **CRITICAL for ES modules:**
use `.js` extensions in import paths even though the source files are `.ts`:

```typescript
import { parseFrame } from './protocol.js';   // NOT './protocol' or './protocol.ts'
import { Outbox } from './outbox.js';
```

Required because we target ES modules (`"module": "es2022"`). Without
`.js` extensions, compiled output fails at runtime with `ERR_MODULE_NOT_FOUND`.

Target ~150-200 lines per file. Single-file submissions are rejected.

## Critical behaviors — easy to miss, MUST be present

These have caused conformance failures in past runs:

1. **Every inbound `deliver` frame MUST be acked with `deliver_ok`.**
   In your message handler, when you receive a `deliver`, do BOTH:
   - Print `[<from> → <to>]  <text>` (two spaces between `]` and text)
   - Send `{"type": "deliver_ok", "id": <id>}` back to the server.
   Without the ack, the server keeps the message in its inbox forever
   and re-delivers on every subsequent login — fails conformance step 6.c.

2. **Use `console.log` for every stdout line — NOT `process.stdout.write`.**
   `console.log` is synchronous and flushed per call. Mixing the two
   risks buffering issues when stdout is piped (the test harness pipes).

3. **Outbox flush serializes per-message.** Wait for `send_ok` before
   sending the next pending row.

4. **Atomic outbox rewrite via `writeFileSync(tmp)` + `renameSync`.**
   LF newlines explicit (`'\n'`).

5. **Handle WebSocket close codes 4000 (superseded) and 4001 (protocol
   error) explicitly** — they drive client state transitions.

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
   - Start a **background async task** (don't block the readline
     loop) that:
     - `await new Promise(r => setTimeout(r, delay))` then try to
       reconnect. Backoff schedule: `1000, 2000, 5000, 5000, ...`
       milliseconds, capped at 10000. Retry indefinitely until
       success or the process exits.
     - On `ws.on('open')`, send `{"type":"login","name":cachedName}`.
     - On `login_ok`: transition to LOGGED_IN. Print exactly
       `reconnected`. Flush the outbox (same procedure as on fresh
       `/login`).
     - On close-code 4000 mid-reconnect: transition to INITIAL,
       clear `cachedName`, print `disconnected: superseded by
       another session`, stop the loop.

   The reconnect task must NOT block stdin — `/send` while OFFLINE
   still has to accept input and queue rows.

8. **WS close code 4000 always halts the reconnect loop.** Whether
   the 4000 arrives while LOGGED_IN (supersede) or during a
   reconnect attempt: clear `cachedName`, transition to INITIAL,
   print `disconnected: superseded by another session`, do NOT
   schedule any further reconnects. The conformance test verifies
   that no `reconnected` line appears after a 4000.

9. **Outbox flushes on EVERY LOGGED_IN entry, not just on
   auto-reconnect.** The same flush procedure runs after a fresh
   `/login` (step 7 of `/login` behavior in the spec) as runs after
   auto-reconnect's `login_ok`. This is what lets a killed-and-
   relaunched process recover pending rows from disk.

10. **Clear the reconnect-loop "running" flag when the loop exits.**
    If you track "is a reconnect loop already running" with a single
    flag/token (to prevent spawning duplicates on rapid disconnects),
    that flag MUST be reset when the loop returns successfully.
    Otherwise a one-shot successful reconnect leaves the flag set;
    the next disconnect's "spawn the loop" call sees `already running`
    and short-circuits — the client stays OFFLINE forever on the
    second cycle. Idiomatic fix in TypeScript:
    ```typescript
    void this.runReconnectLoop(token).finally(() => {
      if (this.reconnectAbort === token) this.reconnectAbort = null;
    });
    ```

## Order of operations

1. Read `spec/spec.md` end to end.
2. Write the four `.ts` modules above.
3. Write `package.json` and `tsconfig.json` (exact contents below).
4. Write `.gitignore` (`node_modules/`, `dist/`, `package-lock.json`).
5. Write `README.md` (~10 lines).
6. Run `npm install` then `npx tsc` in `clients/typescript/`.
7. Verify (see below).
8. Return a one-paragraph summary.

## Other gotchas

- Single event loop. Stdin via `readline.createInterface({input: process.stdin})`, drive with `for await (const line of rl)`.
- Strict TS, no `any`. Inbound frames parsed as `unknown`, narrowed via type guards.
- UUIDs: `crypto.randomUUID()` (Node ≥ 19, built-in).
- No positional CLI args; recognize `--help` and `--version`.
- `MSG_SERVER` env var overrides the default URL.

## Pinned configs (use exact contents)

**`package.json`:**
```json
{
  "name": "messaging-client",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "scripts": { "build": "tsc", "start": "node dist/client.js" },
  "dependencies": { "ws": "^8.16.0" },
  "devDependencies": {
    "@types/node": "^20.0.0",
    "@types/ws": "^8.5.0",
    "typescript": "^5.4.0"
  }
}
```

**`tsconfig.json`:**
```json
{
  "compilerOptions": {
    "target": "es2022",
    "module": "es2022",
    "moduleResolution": "node",
    "esModuleInterop": true,
    "strict": true,
    "outDir": "dist",
    "skipLibCheck": true,
    "resolveJsonModule": true
  },
  "include": ["*.ts"]
}
```

Pinning prevents project-structure drift across runs.

## Verification (run before returning)

```bash
cd clients/typescript && npm install
cd clients/typescript && npx tsc
node clients/typescript/dist/client.js --help
node clients/typescript/dist/client.js --version
```

All four must succeed. `npx tsc` must compile all four `.ts` files to
`dist/*.js` without errors. `--version` must print "messaging-client 1.0.0".

## Return value

On success: one paragraph naming the four module files + verification passed.
On unresolvable failure: `failed: <reason>`.
