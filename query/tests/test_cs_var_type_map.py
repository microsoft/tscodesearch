"""
Unit tests for ``_build_var_type_map`` — the method-scoped variable-name →
resolved-type resolver, and the qualified-call form it produces on each
CallSiteInfo.
"""
from __future__ import annotations

import unittest

from tests.base import _parse
from query.cs import (
    _build_var_type_map,
    _find_all,
    _q_all_call_site_infos,
    _CsIndex,
    _DESCRIBE_NODE_TYPES,
    describe_cs_file,
)


def _node_at(tree, src_bytes, text: str, node_type: str | None = None):
    """Return the first node whose source matches ``text``."""
    for n in _find_all(tree.root_node, lambda x: True):
        if src_bytes[n.start_byte:n.end_byte].decode() == text:
            if node_type is None or n.type == node_type:
                return n
    raise AssertionError(f"node {text!r} not found")


def _call_at(tree, src_bytes, contains: str):
    """Return the first invocation_expression whose text contains ``contains``."""
    for n in _find_all(tree.root_node, lambda x: x.type == "invocation_expression"):
        if contains in src_bytes[n.start_byte:n.end_byte].decode():
            return n
    raise AssertionError(f"call containing {contains!r} not found")


def _vm(src: str):
    b, tree, _ = _parse(src)
    return b, tree, _build_var_type_map(tree, b)


# ── _VarTypeMap ──────────────────────────────────────────────────────────────


class TestParametersResolve(unittest.TestCase):
    def test_method_param_resolved(self):
        src = "class C { void M(Repo r) { r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"

    def test_constructor_param_resolved(self):
        src = "class C { public C(Repo r) { r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"


class TestExplicitLocalsResolve(unittest.TestCase):
    def test_explicit_typed_local(self):
        src = "class C { void M() { Repo r = null; r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"

    def test_var_with_new(self):
        src = "class C { void M() { var r = new Repo(); r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"

    def test_var_with_cast(self):
        src = "class C { void M(object o) { var r = (Repo)o; r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"

    def test_var_with_as_expression(self):
        src = "class C { void M(object o) { var r = o as Repo; r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"

    def test_var_unresolvable_method_call(self):
        # var x = GetRepo() — we don't know GetRepo's return type
        src = "class C { void M() { var r = GetRepo(); r.Save(); } }"
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) is None

    def test_array_element_inferred(self):
        src = """class C {
            void M() {
                Repo[] arr = new Repo[10];
                var r = arr[0];
                r.Save();
            }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        assert vm.resolve_at("r", call) == "Repo"


class TestInferenceHeuristics(unittest.TestCase):
    """Best-guess inference for AI-agent use: await unwrap, generic method
    type args, static factory pattern. False positives are acceptable —
    a wrong qualified form never matches a real call in the AST stage."""

    def test_await_unwraps_to_inner_expression(self):
        src = """class C {
            async void M() {
                var x = await new Repo();
                x.Save();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("x", _call_at(tree, b, "x.Save")) == "Repo"

    def test_generic_method_first_type_arg(self):
        # ``Resolve<T>``, ``Get<T>``, ``As<T>`` style — first type arg is
        # idiomatically the return type.
        src = """class C {
            void M(IContainer c) {
                var s = c.Resolve<IService>();
                s.Run();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("s", _call_at(tree, b, "s.Run")) == "IService"

    def test_bare_generic_method_first_type_arg(self):
        src = """class C {
            void M() {
                var s = Resolve<IService>();
                s.Run();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("s", _call_at(tree, b, "s.Run")) == "IService"

    def test_static_factory_assumes_receiver_type(self):
        # ``Foo.Create()`` is overwhelmingly likely to return Foo (or a
        # subclass). Emitting Foo as the type is the false-positive
        # friendly default.
        src = """class C {
            void M() {
                var w = Widget.Create();
                w.Render();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("w", _call_at(tree, b, "w.Render")) == "Widget"

    def test_factory_skipped_when_receiver_is_known_local_same_block(self):
        # If the PascalCase receiver name is a declared local in the *same*
        # block where var is being inferred, prefer the declaration.
        src = """class C {
            void M() {
                Widget Widget = null;
                var w = Widget.Spawn();
                w.Render();
            }
        }"""
        b, tree, vm = _vm(src)
        # ``Widget`` is a declared local in the same block — the factory
        # heuristic skips and ``w`` stays unresolved (we don't know what
        # an arbitrary instance's ``.Spawn()`` returns).
        assert vm.resolve_at("w", _call_at(tree, b, "w.Render")) is None

    def test_factory_fires_even_when_outer_scope_shadows(self):
        # Inference runs at scope-construction time and doesn't walk outer
        # scopes — a param named the same as a type still lets the factory
        # heuristic fire on inner blocks. False positive is acceptable per
        # the project's AI-agent-friendly bias (worst case: a qualified
        # form that AST post-filter rejects).
        src = """class C {
            void M(Widget Widget) {
                var w = Widget.Spawn();
                w.Render();
            }
        }"""
        b, tree, vm = _vm(src)
        # Best-guess inference fires: ``w`` is treated as ``Widget``.
        assert vm.resolve_at("w", _call_at(tree, b, "w.Render")) == "Widget"

    def test_await_static_factory_combo(self):
        # The patterns compose: ``await TypeName.CreateAsync()`` should
        # still resolve to TypeName.
        src = """class C {
            async void M() {
                var ws = await Workspace.CreateAsync();
                ws.Save();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("ws", _call_at(tree, b, "ws.Save")) == "Workspace"

    def test_await_generic_method_combo(self):
        src = """class C {
            async void M(IContainer c) {
                var s = await c.GetAsync<IService>();
                s.Run();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("s", _call_at(tree, b, "s.Run")) == "IService"


class TestForeachVarIteratorInference(unittest.TestCase):
    """foreach (var x in coll) should derive x's type from coll's element
    type when coll is in scope — fixes a false negative where the iterator
    was left unresolved despite the collection type being statically known."""

    def test_foreach_over_array_field(self):
        src = """class C {
            private Item[] _items;
            void M() {
                foreach (var it in _items) {
                    it.Use();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("it", _call_at(tree, b, "it.Use")) == "Item"

    def test_foreach_over_method_param_list(self):
        src = """class C {
            void M(System.Collections.Generic.List<Item> items) {
                foreach (var it in items) {
                    it.Use();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("it", _call_at(tree, b, "it.Use")) == "Item"

    def test_foreach_over_ienumerable_param(self):
        src = """class C {
            void M(IEnumerable<Item> items) {
                foreach (var it in items) {
                    it.Use();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("it", _call_at(tree, b, "it.Use")) == "Item"

    def test_foreach_over_local_array(self):
        # Local var array — collection lookup walks the parent block scope.
        src = """class C {
            void M() {
                Item[] items = null;
                foreach (var it in items) {
                    it.Use();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("it", _call_at(tree, b, "it.Use")) == "Item"

    def test_foreach_over_dictionary_skipped(self):
        # Dictionary<K,V> has two type args — the iterator is KeyValuePair<K,V>,
        # which we can't summarise as a single PascalCase name. We leave it
        # unresolved rather than emit a wrong guess.
        src = """class C {
            void M(System.Collections.Generic.Dictionary<string, Item> map) {
                foreach (var kv in map) {
                    kv.Key.ToString();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("kv", _call_at(tree, b, "kv.Key.ToString")) is None

    def test_foreach_over_unknown_collection_unresolved(self):
        # Collection name not in scope at all — no inference.
        src = """class C {
            void M() {
                foreach (var x in unknownCollection) {
                    x.Use();
                }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("x", _call_at(tree, b, "x.Use")) is None


class TestPropertyAccessHeuristic(unittest.TestCase):
    """``var x = obj.PascalProperty`` infers x's type as the property name
    (.NET convention that typed sub-objects are named after their type)."""

    def test_property_access_uses_property_name(self):
        src = """class C {
            void M(Context ctx) {
                var meta = ctx.RequestMetadata;
                meta.Touch();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("meta", _call_at(tree, b, "meta.Touch")) == "RequestMetadata"

    def test_property_access_on_this(self):
        src = """class C {
            private Widget _widget;
            void M() {
                var w = this._widget;
                w.Render();
            }
        }"""
        b, tree, vm = _vm(src)
        # ``_widget`` starts with underscore (not Pascal), so the property
        # heuristic skips and the resolver finds nothing for w.
        # Documented limitation: agents searching for Widget.Render on this
        # line miss the qualified form but find the bare ``Render`` call.
        assert vm.resolve_at("w", _call_at(tree, b, "w.Render")) is None

    def test_static_property_uses_receiver_type(self):
        # ``Encoding.UTF8`` — receiver is PascalCase and not in scope, so
        # the static path fires: result type = receiver = Encoding.
        src = """class C {
            void M() {
                var enc = Encoding.UTF8;
                enc.GetBytes(\"x\");
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("enc", _call_at(tree, b, "enc.GetBytes")) == "Encoding"


class TestTernaryInference(unittest.TestCase):
    """``var x = cond ? a : b`` falls back to inferring each branch."""

    def test_ternary_both_object_creation(self):
        src = """class C {
            void M(bool b) {
                var x = b ? new Foo() : new Bar();
                x.Do();
            }
        }"""
        b, tree, vm = _vm(src)
        # First branch wins — both branches independently produce a type;
        # the consequence is tried first.
        assert vm.resolve_at("x", _call_at(tree, b, "x.Do")) == "Foo"

    def test_ternary_property_access_branches(self):
        src = """class C {
            void M(Group group, bool near) {
                var container = near ? group.NearContainer : group.FarContainer;
                container.Use();
            }
        }"""
        b, tree, vm = _vm(src)
        # Both branches are property accesses; the property-name heuristic
        # fires on the first branch.
        assert vm.resolve_at("container", _call_at(tree, b, "container.Use")) == "NearContainer"

    def test_ternary_falls_back_to_second_branch(self):
        # First branch is unresolvable (bare identifier), second is a
        # property access — we try both, the second wins.
        src = """class C {
            void M(bool b, Context ctx) {
                var meta = b ? unknown : ctx.RequestMetadata;
                meta.Touch();
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("meta", _call_at(tree, b, "meta.Touch")) == "RequestMetadata"


class TestFileScopeResolve(unittest.TestCase):
    def test_field_visible_inside_method(self):
        src = """class C {
            private Repo _repo;
            void M() { _repo.Save(); }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "_repo.Save")
        assert vm.resolve_at("_repo", call) == "Repo"

    def test_property_visible_inside_method(self):
        src = """class C {
            public Repo Backend { get; set; }
            void M() { Backend.Save(); }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "Backend.Save")
        assert vm.resolve_at("Backend", call) == "Repo"


class TestMethodScopingIsolation(unittest.TestCase):
    """Same identifier in two methods with different types must not bleed."""

    def test_two_methods_same_name_different_types(self):
        src = """class C {
            void A() { Repo r = null; r.Save(); }
            void B() { Customer r = null; r.Touch(); }
        }"""
        b, tree, vm = _vm(src)
        call_a = _call_at(tree, b, "r.Save")
        call_b = _call_at(tree, b, "r.Touch")
        assert vm.resolve_at("r", call_a) == "Repo"
        assert vm.resolve_at("r", call_b) == "Customer"

    def test_method_local_shadows_field(self):
        src = """class C {
            private Repo r;
            void M() { Customer r = null; r.Touch(); }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Touch")
        assert vm.resolve_at("r", call) == "Customer"


class TestConflictSuppression(unittest.TestCase):
    """Conflicting types in the **same** scope → None (not emitted)."""

    def test_redeclaration_in_same_block_conflicts(self):
        # Two declarations directly in the same block — tree-sitter still
        # parses it; we treat it as ambiguous and emit no qualified form.
        src = """class C {
            void M() {
                Repo x = null;
                x.Save();
                Customer x = null;
                x.Touch();
            }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "x.Save")
        assert vm.resolve_at("x", call) is None


class TestBlockScopingIsolatesBranches(unittest.TestCase):
    """Sibling blocks (if/else, try/catch arms, switch sections) must each
    see their own declarations — this is the false-negative that motivated
    block-level scoping."""

    def test_if_else_branches_isolate_same_name(self):
        src = """class C {
            void M(bool b) {
                if (b) { Repo x = null; x.Save(); }
                else   { Customer x = null; x.Touch(); }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("x", _call_at(tree, b, "x.Save")) == "Repo"
        assert vm.resolve_at("x", _call_at(tree, b, "x.Touch")) == "Customer"

    def test_multiple_catch_clauses_isolate_exception_var(self):
        # Two catches per try-catch with the same variable name is the
        # canonical real-world false-negative this resolves.
        src = """class C {
            void M() {
                try { Do(); }
                catch (Repo ex) { ex.Save(); }
                catch (Customer ex) { ex.Touch(); }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("ex", _call_at(tree, b, "ex.Save")) == "Repo"
        assert vm.resolve_at("ex", _call_at(tree, b, "ex.Touch")) == "Customer"

    def test_for_loop_variable_isolated_per_loop(self):
        src = """class C {
            void M() {
                for (Repo i = null; i != null; i = i.Next) { i.Save(); }
                for (Customer i = null; i != null; i = i.Next) { i.Touch(); }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("i", _call_at(tree, b, "i.Save")) == "Repo"
        assert vm.resolve_at("i", _call_at(tree, b, "i.Touch")) == "Customer"

    def test_foreach_iteration_variable_scoped_to_loop(self):
        src = """class C {
            void M(Repo[] a, Customer[] b) {
                foreach (Repo r in a) { r.Save(); }
                foreach (Customer r in b) { r.Touch(); }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("r", _call_at(tree, b, "r.Save")) == "Repo"
        assert vm.resolve_at("r", _call_at(tree, b, "r.Touch")) == "Customer"

    def test_using_statement_variable_scoped(self):
        src = """class C {
            void M() {
                using (Repo s = null) { s.Save(); }
                using (Customer s = null) { s.Touch(); }
            }
        }"""
        b, tree, vm = _vm(src)
        assert vm.resolve_at("s", _call_at(tree, b, "s.Save")) == "Repo"
        assert vm.resolve_at("s", _call_at(tree, b, "s.Touch")) == "Customer"


class TestNestedScopes(unittest.TestCase):
    def test_lambda_inherits_enclosing_method_var(self):
        src = """class C {
            void M() {
                Repo r = null;
                System.Action a = () => r.Save();
            }
        }"""
        b, tree, vm = _vm(src)
        call = _call_at(tree, b, "r.Save")
        # ``r`` is declared in the method body, not the lambda; the resolver
        # walks up from the call to the lambda (no ``r``), then to the
        # method (has ``r``), then returns ``Repo``.
        assert vm.resolve_at("r", call) == "Repo"

    def test_typed_lambda_param_resolved(self):
        # Typed lambda parameters land in a regular parameter_list — same
        # handling as a method parameter.
        src = """class C {
            void M(Repo r) {
                System.Func<Customer, int> a = (Customer c) => c.Id;
            }
        }"""
        b, tree, vm = _vm(src)
        member = _node_at(tree, b, "c.Id", "member_access_expression")
        assert vm.resolve_at("c", member) == "Customer"
        # Outer-method param still visible from the lambda body.
        assert vm.resolve_at("r", member) == "Repo"

    def test_implicit_lambda_param_unresolved(self):
        # ``c => c.Id`` carries no syntactic type — resolution would need
        # cross-expression Func<,> inference, which we don't do. The
        # resolver returns None rather than guessing.
        src = """class C {
            void M() {
                System.Func<Customer, int> a = c => c.Id;
            }
        }"""
        b, tree, vm = _vm(src)
        member = _node_at(tree, b, "c.Id", "member_access_expression")
        assert vm.resolve_at("c", member) is None


# ── _q_all_call_site_infos integration ───────────────────────────────────────


class TestCallSiteResolvedType(unittest.TestCase):
    """Verify ``resolved_type`` is set on CallSiteInfo when receiver resolves."""

    def _infos(self, src: str):
        b, tree, vm = _vm(src)
        idx = _CsIndex(b, tree, _DESCRIBE_NODE_TYPES)
        return _q_all_call_site_infos(b, idx, vm)

    def test_resolved_for_typed_local(self):
        infos = self._infos("class C { void M() { Repo r = null; r.Save(); } }")
        save = next(cs for cs in infos if cs.name == "Save")
        assert save.receiver == "r"
        assert save.resolved_type == "Repo"

    def test_not_resolved_for_unknown_local(self):
        infos = self._infos("class C { void M() { var r = GetRepo(); r.Save(); } }")
        save = next(cs for cs in infos if cs.name == "Save")
        assert save.receiver == "r"
        assert save.resolved_type == ""

    def test_pascal_receiver_static_call(self):
        # ``Foo.Save()`` — Foo isn't a declared variable, so var-type map
        # returns None; resolved_type is empty. The literal receiver
        # captures Foo for the indexer's static-style qualifying.
        infos = self._infos("class C { void M() { Foo.Save(); } }")
        save = next(cs for cs in infos if cs.name == "Save")
        assert save.receiver == "Foo"
        assert save.resolved_type == ""

    def test_resolved_uses_unqualified_type(self):
        # ``A.B.Repo`` field type → resolved_type stored as ``Repo``
        # so the qualified-call form is stable regardless of namespace.
        infos = self._infos(
            "class C { private A.B.Repo _r; void M() { _r.Save(); } }"
        )
        save = next(cs for cs in infos if cs.name == "Save")
        assert save.resolved_type == "Repo"

    def test_resolved_strips_generics(self):
        infos = self._infos(
            "class C { void M() { List<int> xs = null; xs.Add(1); } }"
        )
        add = next(cs for cs in infos if cs.name == "Add")
        assert add.resolved_type == "List"


# ── q_calls qualified-receiver matching ──────────────────────────────────────


class TestQCallsQualifiedReceiver(unittest.TestCase):
    """``q_calls("Type.Method")`` must match call sites whose receiver
    resolves to ``Type`` via the var-type map, not just those whose literal
    receiver text equals ``Type``."""

    def _q(self, src: str, pattern: str):
        from query.cs import q_calls
        b, tree, _ = _parse(src)
        return q_calls(b, tree, src.splitlines(), pattern)

    def test_literal_receiver_match_unchanged(self):
        # ``Foo.Save()`` — receiver is literally "Foo"; legacy behaviour.
        r = self._q("class C { void M() { Foo.Save(); } }", "Foo.Save")
        assert len(r) == 1, r

    def test_typed_local_receiver_matches_via_resolved_type(self):
        # ``r: Repo`` then ``r.Save()`` — should match q_calls("Repo.Save").
        r = self._q(
            "class C { void M() { Repo r = null; r.Save(); } }",
            "Repo.Save")
        assert len(r) == 1, r

    def test_field_receiver_matches_via_resolved_type(self):
        # Field declaration in file scope; method-local call uses it.
        r = self._q(
            "class C { private IRepo _repo; void M() { _repo.Save(); } }",
            "IRepo.Save")
        assert len(r) == 1, r

    def test_param_receiver_matches_via_resolved_type(self):
        r = self._q(
            "class C { void M(IRepo repo) { repo.Save(); } }",
            "IRepo.Save")
        assert len(r) == 1, r

    def test_unresolved_receiver_rejects_qualified_match(self):
        # ``var r = GetRepo()`` is unresolvable; q_calls("Repo.Save") must
        # not match (the qualifier check fails on literal and on resolution).
        r = self._q(
            "class C { void M() { var r = GetRepo(); r.Save(); } }",
            "Repo.Save")
        assert r == [], r

    def test_conflict_suppressed_receiver_rejects_qualified_match(self):
        # Two same-name locals of different types in one block → conflict →
        # no qualified match emitted. The bare-name search still finds them.
        src = """class C {
            void M() {
                Repo x = null;
                x.Save();
                Customer x = null;
                x.Save();
            }
        }"""
        # Qualified form should NOT match (resolution is ambiguous).
        r_qual = self._q(src, "Repo.Save")
        assert r_qual == [], r_qual
        # Bare-name should still find both calls.
        r_bare = self._q(src, "Save")
        assert len(r_bare) == 2, r_bare

    def test_null_conditional_uses_resolved_type(self):
        # ``r?.Save()`` where r resolves to Repo.
        r = self._q(
            "class C { void M(Repo r) { r?.Save(); } }",
            "Repo.Save")
        assert len(r) == 1, r

    def test_literal_does_not_match_when_resolution_differs(self):
        # ``r: Repo`` then ``r.Save()`` — must NOT match q_calls("OtherType.Save")
        # since neither literal "OtherType" nor resolved type matches.
        r = self._q(
            "class C { void M() { Repo r = null; r.Save(); } }",
            "OtherType.Save")
        assert r == [], r


# ── q_calls anchors chained calls at the name token ──────────────────────────


class TestQCallsChainedReporting(unittest.TestCase):
    """For multi-line chained calls ``a.B().Method(...)``, the reported line
    should be where ``Method`` itself appears — not the start of the outer
    invocation. The reported text is the single source line at that row."""

    def _q(self, src: str, pattern: str):
        from query.cs import q_calls
        b, tree, _ = _parse(src)
        return q_calls(b, tree, src.splitlines(), pattern)

    def test_single_line_call_unchanged(self):
        # ``Foo.Save();`` — name and call start on the same row; result row
        # is the obvious line (regression check that the new anchor logic
        # doesn't shift simple cases).
        src = "class C { void M() { Foo.Save(); } }"
        r = self._q(src, "Save")
        assert len(r) == 1, r
        line, text = r[0]
        assert line == 1
        assert "Foo.Save()" in text

    def test_chained_call_reports_name_line_not_chain_start(self):
        # The outer invocation spans two lines (chain starts at L2, the
        # ``ConfigureAwait`` name token sits on L3). The reported line
        # should be L3 — the row containing the matched identifier.
        src = (
            "class C { void M(System.Threading.Tasks.Task<int> task) {\n"
            "    task.Result\n"
            "        .ToString();\n"
            "} }\n"
        )
        r = self._q(src, "ToString")
        assert len(r) == 1, r
        line, text = r[0]
        assert line == 3, f"expected name-token line 3, got {line}: {r}"
        # The reported text is the single line containing ToString, not
        # the multi-line node render.
        assert ".ToString()" in text
        assert "task.Result" not in text

    def test_chained_call_at_method_call_receiver(self):
        # Receiver of the outer ``.Save()`` is itself an invocation
        # (``Get()``). The outer invocation begins at L2 (``Get`` line),
        # but ``Save`` is on L3.
        src = (
            "class C { void M() {\n"
            "    this.Get()\n"
            "        .Save();\n"
            "} }\n"
        )
        r = self._q(src, "Save")
        assert len(r) == 1, r
        line, _ = r[0]
        assert line == 3, f"expected L3 (Save's line), got {line}: {r}"


# ── describe_cs_file end-to-end ──────────────────────────────────────────────


class TestDescribeFileEnd2End(unittest.TestCase):
    """describe_cs_file must produce CallSiteInfo with resolved_type."""

    def test_resolved_type_propagates(self):
        src = b"class C { void M() { Repo r = null; r.Save(); } }"
        fd = describe_cs_file(src)
        save = next(cs for cs in fd.call_site_infos if cs.name == "Save")
        assert save.resolved_type == "Repo"


if __name__ == "__main__":
    unittest.main()
