"""Conformance test runner.

Discovers test cases under test/cases/, runs them sequentially,
reports PASS or FAIL with the first failing case + reason.

Each cases/case_NN_*.py module exposes a `cases()` function returning
the ordered list of async callables it contributes. This runner
collects them in iteration order and walks the combined list.

Usage:
    python test/runner.py --client-cmd "python clients/python/client.py"
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Make `from framework import ...` and `from cases.X import ...` work
# whether invoked as `python test/runner.py` or `python -m test.runner`.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from framework import A, fail  # noqa: E402  (sys.path tweak required first)
from cases.case_01_login import cases as cases_01_login  # noqa: E402
from cases.case_02_send import cases as cases_02_send  # noqa: E402
from cases.case_03_offline import cases as cases_03_offline  # noqa: E402
from cases.case_04_outbox_queue import cases as cases_04_outbox_queue  # noqa: E402


def collect_cases():
    """Concatenate every iteration's contribution in order."""
    return [
        *cases_01_login(),
        *cases_02_send(),
        *cases_03_offline(),
        *cases_04_outbox_queue(),
    ]


async def main_async(args) -> int:
    cases = collect_cases()
    print(f"{A.BOLD}═══ conformance test ({len(cases)} cases) ═══{A.RESET}\n")
    for fn in cases:
        try:
            await fn(args.client_cmd)
        except (AssertionError, TimeoutError) as e:
            print()
            fail(str(e))
            return 1
        except Exception as e:
            print()
            fail(f"unexpected error: {type(e).__name__}: {e}")
            return 2
    print()
    print(f"{A.GREEN}{A.BOLD}═══ PASS ═══{A.RESET}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Conformance test runner.")
    parser.add_argument(
        "--client-cmd",
        required=True,
        help='Shell command to launch a client, e.g., "python clients/python/client.py"',
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
