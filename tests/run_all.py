#!/usr/bin/env python3
"""Run the whole test suite: python3 -m tests.run_all"""

from __future__ import annotations

import sys

from tests import test_api, test_index, test_textrules
from tests.harness import run_cases


def main() -> int:
    cases = []
    for module in (test_textrules, test_index, test_api):
        cases.extend(module.CASES)
    return run_cases(cases)


if __name__ == "__main__":
    sys.exit(main())
