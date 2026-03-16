"""
Tests for params mode.

mode: --params METHOD (q_params)

Shows the full parameter list (types, names, defaults, modifiers) of every
method/constructor/local function named METHOD.

Gaps tested:
  - Parameter types and names are shown.
  - Default values are included in output.
  - ref/out/in modifiers are shown.
  - Methods with no parameters return "(no parameters)".
  - Same name in multiple methods returns multiple results.
  - Constructors are included.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from tests.fixtures import PARAMS_TARGET
from query import q_params


# ══════════════════════════════════════════════════════════════════════════════
# q_params AST function
# ══════════════════════════════════════════════════════════════════════════════

class TestQParams(unittest.TestCase):

    def _params(self, src, method):
        return q_params(*_parse(src), method_name=method)

    def test_finds_method_params(self):
        r = self._params(PARAMS_TARGET, "SimpleMethod")
        assert r, "SimpleMethod must be found"

    def test_shows_param_types(self):
        r = self._params(PARAMS_TARGET, "SimpleMethod")
        texts = "\n".join(t for _, t in r)
        assert "string" in texts, f"Param type 'string' missing: {texts}"
        assert "int"    in texts, f"Param type 'int' missing: {texts}"
        assert "bool"   in texts, f"Param type 'bool' missing: {texts}"

    def test_shows_param_names(self):
        r = self._params(PARAMS_TARGET, "SimpleMethod")
        texts = "\n".join(t for _, t in r)
        assert "key"   in texts, f"Param name 'key' missing: {texts}"
        assert "count" in texts, f"Param name 'count' missing: {texts}"

    def test_shows_defaults(self):
        r = self._params(PARAMS_TARGET, "WithDefaults")
        texts = "\n".join(t for _, t in r)
        assert "10"    in texts, f"Default value '10' missing: {texts}"
        assert "false" in texts, f"Default value 'false' missing: {texts}"

    def test_shows_modifiers(self):
        r = self._params(PARAMS_TARGET, "WithModifiers")
        texts = "\n".join(t for _, t in r)
        assert "ref" in texts, f"ref modifier missing: {texts}"
        assert "out" in texts, f"out modifier missing: {texts}"

    def test_no_params_method(self):
        r = self._params(PARAMS_TARGET, "NoParams")
        assert r, "NoParams must be found"
        texts = "\n".join(t for _, t in r)
        assert "no parameters" in texts.lower(), \
            f"No-param method must say '(no parameters)': {texts}"

    def test_constructor_params_found(self):
        r = self._params(PARAMS_TARGET, "ParamsDemo")
        assert r, "Constructor ParamsDemo must be found"
        texts = "\n".join(t for _, t in r)
        assert "name" in texts and "log" in texts.lower()

    def test_nonexistent_method_empty(self):
        r = self._params(PARAMS_TARGET, "NonExistent")
        assert r == []

    def test_same_name_multiple_methods(self):
        src = """\
namespace Synth {
    public class A {
        public void Process(string key) { }
    }
    public class B {
        public void Process(int count, bool flag) { }
    }
}
"""
        r = self._params(src, "Process")
        assert len(r) == 2, \
            f"Both overloads of 'Process' must be found, got {len(r)}"

    def test_local_function_params_found(self):
        src = """\
namespace Synth {
    public class Worker {
        public void Run() {
            void Inner(string key, int max) { }
        }
    }
}
"""
        r = self._params(src, "Inner")
        assert r, "Local function params must be found"
        texts = "\n".join(t for _, t in r)
        assert "key" in texts and "max" in texts

    def test_generic_param_shown(self):
        src = """\
namespace Synth {
    public class Svc {
        public void Register(IList<Widget> items) { }
    }
}
"""
        r = self._params(src, "Register")
        assert r, "Generic param must be shown"
        texts = "\n".join(t for _, t in r)
        assert "IList" in texts or "Widget" in texts


if __name__ == "__main__":
    unittest.main()
