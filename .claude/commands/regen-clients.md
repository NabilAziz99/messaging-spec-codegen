---
description: Regenerate both clients from spec/spec.md and verify they pass the conformance test.
---

# /regen-clients

Orchestrator for full regeneration. Spawn sub-agents to generate
Python + TypeScript clients in parallel, then run the conformance test.

## Preconditions

Abort with a clear error if any fail:

1. `spec/spec.md` exists and is non-empty.
2. `server/server.py`, `test/runner.py`, and `test/framework.py` exist.
3. `clients/python/`, `clients/typescript/` are empty or absent. If
   non-empty, ask the user before overwriting.

Announce: `preconditions ok. spec: spec/spec.md → clients/{python,typescript}/`

## Workflow

### Phase 1 — Generate both clients (parallel)

Single message, two Agent calls (parallel via the dispatcher's safe-batch):

- `python-client-generator`: *"Generate clients/python/ from spec/spec.md. Verify and return a summary."*
- `typescript-client-generator`: *"Generate clients/typescript/ from spec/spec.md. Verify and return a summary."*

If either returns `failed: …`, abort and surface verbatim.

### Phase 2 — Verify conformance

Spawn `conformance-checker`. Prompt:

> Run `python test/runner.py --client-cmd "python clients/python/client.py"`. Report PASS or FAIL with the failing case.

### Phase 3 — Report

```
═══ /regen-clients result ═══
  Phase 1 — generation:  python: <files>
                         typescript: <files>
  Phase 2 — conformance: PASS|FAIL
═══════════════════════════════
```

## Rules

- **Delegate.** Don't write client code in the orchestrator turn.
- **Never edit** `spec/spec.md`, `server/`, or `test/`.
- **Fail loudly.** Any phase failure aborts with a clear message.
- The two generators run in parallel; they have independent contexts
  and cannot see each other's output.
