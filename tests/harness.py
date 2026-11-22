"""Test harness: tiny assertion helpers, no third-party test framework."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


class Case:
    def __init__(self, name: str):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def eq(actual, expected, message=""):
    if actual != expected:
        raise AssertionError("%s\n  expected: %r\n  actual:   %r" % (message, expected, actual))


def approx(actual, expected, tol=1e-9, message=""):
    if abs(float(actual) - float(expected)) > tol:
        raise AssertionError("%s\n  expected: %r ± %r\n  actual:   %r"
                             % (message, expected, tol, actual))


def ok(cond, message=""):
    if not cond:
        raise AssertionError(message or "assertion failed")


def contains(haystack, needle, message=""):
    if needle not in haystack:
        raise AssertionError("%s\n  %r not found in %r" % (message, needle, haystack))


def make_tmp(prefix: str = "corpuslake_test_") -> Path:
    return Path(tempfile.mkdtemp(prefix=prefix))


def cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def run_cases(cases):
    failures = []
    for name, fn in cases:
        try:
            fn()
            print("ok   %s" % name)
        except Exception:
            failures.append((name, traceback.format_exc()))
            print("FAIL %s" % name)
    if failures:
        print("\n%d failure(s):" % len(failures))
        for name, tb in failures:
            print("\n--- %s ---\n%s" % (name, tb))
        return 1
    print("\nall %d test(s) passed" % len(cases))
    return 0


class TestClient:
    """Minimal JSON client for the test server."""

    def __init__(self, base: str):
        self.base = base

    def request(self, method: str, path: str, body=None):
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "application/json" in ctype:
                    return resp.status, json.loads(raw.decode("utf-8"))
                return resp.status, raw
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except Exception:
                payload = raw
            return exc.code, payload

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, body=None):
        return self.request("POST", path, body or {})
