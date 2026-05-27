# Python client

CLI messaging client for the spec in `spec/spec.md` (iteration 1: login).
Reads commands from stdin, writes status lines to stdout.

## Install

    pip install -r requirements.txt

## Run

    python client.py

Then type `/login <name>`, `/help`, or `/quit`. The server must be running at
`ws://127.0.0.1:8765/ws`.
