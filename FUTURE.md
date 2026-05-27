# Future work

Trade-offs deferred from v1. Each is referenced from a corresponding
section in DESIGN.md.

## Authentication

Login auto-registers; anyone can claim any name. Production needs JWT
or session tokens with proof-of-identity at login.

## Encryption

The WebSocket carries plaintext. The server sees every message body.
End-to-end encryption would change the threat model entirely
(server becomes opaque relay; identity keys per user).

## Unbounded server inbox

`inbox[recipient]` has no TTL or size cap. A long-offline user
accumulates messages indefinitely. Real systems evict on a policy
(time, size, or both) or persist to disk for cold storage.

## Mid-send-disconnect

If the WebSocket dies between sending a `send` frame and receiving
its `send_ok`, the message is lost — neither in the outbox (live
sends don't persist) nor confirmed by the server. An in-memory
pending set, flushed to the outbox on connection drop, would close
the gap.

## Strict validation

Frames with unknown top-level keys are rejected. Future protocol
versions can't add fields without breaking older peers. A versioned
handshake (declared compatibility level at login) would unlock
additive change.

## Multi-device per user

A second login as the same name kicks the first (code 4000). Real
chat apps fan out to all of a user's devices, with sync of read-state
across them.

## Server persistence

In-memory only — restart wipes everything. Brief explicitly allows
this; production would back the inbox + seen_ids with SQLite or
similar.

## Out of brief

Group chat, read receipts, message edit/delete, rate limiting,
typing indicators. Not in scope; listed so a reviewer sees what's
missing for a production messenger.
