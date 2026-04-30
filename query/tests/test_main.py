"""
Tests for query/__main__.py — both CLI and --json modes.

Runs query as a subprocess so the full entry-point path is exercised.
Uses sample files from sample/root1/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SAMPLE = os.path.join(_REPO, "sample", "root1")
_DATASTORE = os.path.join(_SAMPLE, "DataStore.cs")
_PIPELINE  = os.path.join(_SAMPLE, "pipeline.py")


def _cli(*args) -> subprocess.CompletedProcess:
    """Run  python -m query <args>  and return the completed process."""
    return subprocess.run(
        [sys.executable, "-m", "query", *args],
        capture_output=True, text=True, cwd=_REPO,
    )


def _json_mode(payload: dict) -> subprocess.CompletedProcess:
    """Run  python -m query --json  with payload as stdin."""
    return subprocess.run(
        [sys.executable, "-m", "query", "--json"],
        input=json.dumps(payload), capture_output=True, text=True, cwd=_REPO,
    )


def _matches(proc: subprocess.CompletedProcess) -> list[dict]:
    return json.loads(proc.stdout)["matches"]


class TestCliMode(unittest.TestCase):

    def test_methods_returns_matches(self):
        proc = _cli("--mode", "methods", "--file", _DATASTORE)
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "methods mode must return at least one match"

    def test_methods_texts_contain_signatures(self):
        proc = _cli("--mode", "methods", "--file", _DATASTORE)
        texts = [r["text"] for r in _matches(proc)]
        assert any("Write" in t for t in texts)
        assert any("Read"  in t for t in texts)

    def test_matches_have_line_and_text(self):
        proc = _cli("--mode", "methods", "--file", _DATASTORE)
        for m in _matches(proc):
            assert "line" in m and isinstance(m["line"], int)
            assert "text" in m and isinstance(m["text"], str)

    def test_calls_with_pattern(self):
        proc = _cli("--mode", "calls", "--pattern", "Write", "--file", _DATASTORE)
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "calls mode must find Write call site"
        assert any("Write" in r["text"] for r in m)

    def test_uses_with_pattern(self):
        proc = _cli("--mode", "uses", "--pattern", "IDataStore", "--file", _DATASTORE)
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "uses mode must find IDataStore references"
        assert any("IDataStore" in r["text"] for r in m)

    def test_no_matches_returns_empty_list(self):
        proc = _cli("--mode", "calls", "--pattern", "NoSuchMethod", "--file", _DATASTORE)
        assert proc.returncode == 0
        assert _matches(proc) == []

    def test_missing_file_arg_exits_nonzero(self):
        proc = _cli("--mode", "methods")
        assert proc.returncode != 0

    def test_missing_mode_arg_exits_nonzero(self):
        proc = _cli("--file", _DATASTORE)
        assert proc.returncode != 0

    def test_nonexistent_file_returns_error(self):
        proc = _cli("--mode", "methods", "--file", "/nonexistent/Missing.cs")
        assert proc.returncode != 0
        result = json.loads(proc.stdout)
        assert "error" in result

    def test_python_file(self):
        proc = _cli("--mode", "methods", "--file", _PIPELINE)
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "methods mode must return functions from a Python file"

    def test_output_is_valid_json(self):
        proc = _cli("--mode", "methods", "--file", _DATASTORE)
        result = json.loads(proc.stdout)
        assert "matches" in result


class TestJsonMode(unittest.TestCase):

    def test_methods_returns_matches(self):
        proc = _json_mode({"mode": "methods", "file": _DATASTORE})
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "--json methods must return matches"

    def test_calls_with_pattern(self):
        proc = _json_mode({"mode": "calls", "pattern": "Write", "file": _DATASTORE})
        assert proc.returncode == 0
        m = _matches(proc)
        assert m
        assert any("Write" in r["text"] for r in m)

    def test_uses_kind_param(self):
        proc = _json_mode({
            "mode": "uses", "pattern": "IDataStore",
            "uses_kind": "param", "file": _DATASTORE,
        })
        assert proc.returncode == 0
        m = _matches(proc)
        assert m, "uses/param must find IDataStore parameters"
        assert all("IDataStore" in r["text"] for r in m)

    def test_missing_file_key_returns_error(self):
        proc = _json_mode({"mode": "methods"})
        assert proc.returncode != 0
        result = json.loads(proc.stdout)
        assert "error" in result

    def test_bad_json_stdin_returns_error(self):
        proc = subprocess.run(
            [sys.executable, "-m", "query", "--json"],
            input="not json", capture_output=True, text=True, cwd=_REPO,
        )
        assert proc.returncode != 0
        result = json.loads(proc.stdout)
        assert "error" in result

    def test_nonexistent_file_returns_error(self):
        proc = _json_mode({"mode": "methods", "file": "/nonexistent/Missing.cs"})
        assert proc.returncode != 0
        result = json.loads(proc.stdout)
        assert "error" in result

    def test_output_is_valid_json(self):
        proc = _json_mode({"mode": "methods", "file": _DATASTORE})
        result = json.loads(proc.stdout)
        assert "matches" in result


class TestModesAgree(unittest.TestCase):
    """CLI and --json modes must produce identical results for the same query."""

    def _compare(self, mode, file, **kwargs):
        cli_args = ["--mode", mode, "--file", file]
        payload  = {"mode": mode, "file": file}
        for k, v in kwargs.items():
            cli_args += [f"--{k.replace('_', '-')}", v]
            payload[k] = v
        cli  = _matches(_cli(*cli_args))
        via_json = _matches(_json_mode(payload))
        assert cli == via_json, f"CLI and --json disagree for mode={mode}"

    def test_methods_agree(self):
        self._compare("methods", _DATASTORE)

    def test_calls_agree(self):
        self._compare("calls", _DATASTORE, pattern="Write")

    def test_uses_agree(self):
        self._compare("uses", _DATASTORE, pattern="IDataStore")

    def test_python_methods_agree(self):
        self._compare("methods", _PIPELINE)


if __name__ == "__main__":
    unittest.main()
