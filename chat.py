#!/usr/bin/env python3
"""Two-party chat wrapper around a spec-compliant client.

The spec'd clients use `/send <recipient> <text>` for every message —
correct at the wire level (every message must be addressed) but clunky
in a 2-party demo where there's only one possible recipient. This
wrapper sits in front of a client and:

  - asks you who you are and who you're talking to (or accepts both
    as positional args if you'd rather skip the prompts)
  - logs in as <my-name> automatically
  - prints a friendly banner so it's obvious what to do
  - shows a `<my> → <their> > ` prompt before each input
  - translates plain stdin lines into `/send <their-name> <line>`
    before piping to the client
  - lets `/`-prefixed lines pass through (so /help and /quit still
    work). Offline / online state is driven by the network, not by
    commands — kill the server and you'll see `disconnected`; bring
    it back and you'll see `reconnected`.

The wire protocol and the underlying client are unchanged — this is
purely a rendering layer over the spec-compliant CLI. Hand-written;
not part of the regeneration contract.

Usage:
    python chat.py                       # interactive — prompts for names
    python chat.py alice bob             # skip the prompts
    python chat.py alice bob --ts        # wrap the TypeScript client
"""
from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys

CLIENT_BY_LANG = {
    "python": "python clients/python/client.py",
    "py":     "python clients/python/client.py",
    "typescript": "node clients/typescript/dist/client.js",
    "ts":     "node clients/typescript/dist/client.js",
}
DEFAULT_CLIENT = CLIENT_BY_LANG["python"]

# Must match the spec's name validation rule (NAME_RE in the clients
# and NAME_PATTERN on the server). Names that don't pass this would be
# rejected by the underlying client anyway — we check up front so the
# user gets a clear prompt-time error rather than a confusing
# `error: usage: /login <name>` after the banner.
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

BANNER = """
─────────────────────────────────────────────────────────────
  Connected as: {my}    →    Talking to: {their}
─────────────────────────────────────────────────────────────
  Type a message and hit Enter.
  Slash-commands work too:  /help  /quit
  (Offline / online is driven by the network — not commands.)
─────────────────────────────────────────────────────────────
""".strip()


def prompt_name(question: str) -> str:
    """Ask for a name on stdin, validating against the spec's name regex.

    Loops until a valid name is given or the user hits ^D / ^C (which
    propagates and the caller exits)."""
    while True:
        name = input(question).strip()
        if NAME_RE.match(name):
            return name
        print(
            f"  '{name}' isn't a valid name. Use letters, digits, '_' or '-' "
            f"(1-32 chars).",
            file=sys.stderr,
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "my_name", nargs="?", default=None,
        help="your username (prompted if omitted)",
    )
    parser.add_argument(
        "their_name", nargs="?", default=None,
        help="the recipient's username (prompted if omitted)",
    )
    lang_group = parser.add_mutually_exclusive_group()
    lang_group.add_argument(
        "--py", action="store_const", const="py", dest="lang",
        help="wrap the Python client (default)",
    )
    lang_group.add_argument(
        "--ts", action="store_const", const="ts", dest="lang",
        help="wrap the TypeScript client",
    )
    parser.set_defaults(lang="py")
    parser.add_argument(
        "--client",
        default=None,
        help="advanced: override --py/--ts with a custom client command",
    )
    args = parser.parse_args()
    client_cmd = args.client if args.client is not None else CLIENT_BY_LANG[args.lang]

    # Fill in any names the user didn't pass on the CLI.
    try:
        my_name = args.my_name or prompt_name("Your name? ")
        their_name = args.their_name or prompt_name("Who do you want to chat with? ")
    except (EOFError, KeyboardInterrupt):
        print()  # newline so the next shell prompt looks clean
        return 0

    print(BANNER.format(my=my_name, their=their_name))

    proc = subprocess.Popen(
        shlex.split(client_cmd),
        stdin=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

    # Auto-login on launch.
    proc.stdin.write(f"/login {my_name}\n")
    proc.stdin.flush()

    prompt = f"{my_name} → {their_name} > "

    try:
        while proc.poll() is None:
            try:
                line = input(prompt)
            except EOFError:
                break

            if not line:
                continue

            if line == "/quit":
                proc.stdin.write("/quit\n")
                proc.stdin.flush()
                break

            if line.startswith("/"):
                proc.stdin.write(line + "\n")
            else:
                # Local echo so the sender sees their own message in the
                # transcript, then forward as a /send to the spec'd client.
                print(f"[{my_name} → {their_name}]  {line}", flush=True)
                proc.stdin.write(f"/send {their_name} {line}\n")
            proc.stdin.flush()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait()
    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
