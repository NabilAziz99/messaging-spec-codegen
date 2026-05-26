# Spec roadmap

This spec is built **iteratively**, one feature per commit. Each
iteration ships a feature with its own error handling and conformance
tests; we don't move to the next iteration until the current one is
green.

The whole spec lives in a single file: `spec.md`. Each iteration
appends its sections (behavior, frames, errors, conformance test) to
that file. The roadmap below tracks which iterations have landed.

## Status

| # | Feature | Status |
|---|---|---|
| 1 | Login + name validation             | ✅ done (5/5 cases pass) |
| 2 | Online send + receive               | ✅ done (5/5 cases pass) |
| 3 | Offline state (no outbox yet)       | ✅ done (4/4 cases pass) |
| 4 | Outbox queue (offline `/send`)      | ✅ done (3/3 cases pass) |
| 5 | Outbox flush on reconnect           | ✅ done (2/2 cases pass) |
| 6 | Server inbox (offline recipient)    | ✅ done (3/3 cases pass) |
| 7 | Concurrent flush + push (reconnect) | ✅ done (brief's 7-step scenario passes) |
| 8 | UUID dedup + retry safety           | ✅ done (2/2 cases pass) |
| 9 | Protocol error handling             | ✅ done (4/4 cases pass) |
| 10 | Polish (`/help`, `/quit`, version) | ✅ done (3/3 cases pass) |

## See also

- `spec.md` — the actual spec (grows with each iteration)
- `../DESIGN.md` — design decisions and reasoning
- `../README.md` — top-level project intro
