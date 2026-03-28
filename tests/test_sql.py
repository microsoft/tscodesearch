"""
Tests for SQL metadata extraction — covers tables, views, stored
procedures, functions, column types, and table references.
"""
import os
import sys
import pytest
import tree_sitter_sql

_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _base not in sys.path:
    sys.path.insert(0, _base)

FIXTURE = os.path.join(os.path.dirname(__file__), "sql_fixture.sql")


@pytest.fixture
def fixture_bytes():
    with open(FIXTURE, "rb") as f:
        return f.read()


# ── extract_sql_metadata tests ────────────────────────────────────────────────

class TestExtractSqlMetadata:
    """Unit tests for the indexer's extract_sql_metadata()."""

    @pytest.fixture(autouse=True)
    def _load(self, fixture_bytes):
        from indexserver.indexer import extract_sql_metadata
        self.meta = extract_sql_metadata(fixture_bytes)

    def test_tables_found(self):
        """CREATE TABLE names should appear in class_names."""
        cn = self.meta["class_names"]
        assert "dbo.Products" in cn or "Products" in cn
        assert "dbo.Orders" in cn or "Orders" in cn

    def test_view_found(self):
        """CREATE VIEW name should appear in class_names."""
        cn = self.meta["class_names"]
        assert "dbo.ActiveProducts" in cn or "ActiveProducts" in cn

    def test_procs_found(self):
        """Stored procedure names should appear in method_names."""
        mn = self.meta["method_names"]
        proc_names_found = [n for n in mn if "proc_GetProductById" in n]
        assert len(proc_names_found) > 0, f"proc_GetProductById not found in {mn}"

    def test_proc_insert_order_found(self):
        mn = self.meta["method_names"]
        proc_names_found = [n for n in mn if "proc_InsertOrder" in n]
        assert len(proc_names_found) > 0, f"proc_InsertOrder not found in {mn}"

    def test_create_or_alter_proc(self):
        """CREATE OR ALTER PROCEDURE should be found by regex fallback."""
        mn = self.meta["method_names"]
        proc_names_found = [n for n in mn if "proc_UpdateProduct" in n]
        assert len(proc_names_found) > 0, f"proc_UpdateProduct not found in {mn}"

    def test_function_found(self):
        """CREATE FUNCTION name should appear in method_names."""
        mn = self.meta["method_names"]
        func_names_found = [n for n in mn if "fn_GetProductCount" in n]
        assert len(func_names_found) > 0, f"fn_GetProductCount not found in {mn}"

    def test_column_types(self):
        """Column types should appear in type_refs."""
        tr = [t.upper() for t in self.meta["type_refs"]]
        found_any = any(t in tr for t in ["UNIQUEIDENTIFIER", "NVARCHAR(256)", "INT", "DATETIME", "BIT"])
        assert found_any, f"No column types found in {tr}"

    def test_referenced_tables(self):
        """Tables referenced in FROM/JOIN/REFERENCES should appear in call_sites."""
        cs = self.meta["call_sites"]
        ref_found = [n for n in cs if "Products" in n]
        assert len(ref_found) > 0, f"No Products reference found in call_sites: {cs}"


# ── AST helper tests ──────────────────────────────────────────────────────────

class TestSqlAstHelpers:
    """Tests for src/ast/sql.py helper functions."""

    @pytest.fixture(autouse=True)
    def _parse(self, fixture_bytes):
        from tree_sitter import Language, Parser
        lang = Language(tree_sitter_sql.language())
        parser = Parser(lang)
        self.tree = parser.parse(fixture_bytes)
        self.root = self.tree.root_node
        self.src = fixture_bytes

    def test_extract_table_names(self):
        from src.ast.sql import extract_table_names
        names = extract_table_names(self.root, self.src)
        assert any("Products" in n for n in names)
        assert any("Orders" in n for n in names)

    def test_extract_function_names(self):
        from src.ast.sql import extract_function_names
        names = extract_function_names(self.root, self.src)
        assert any("fn_GetProductCount" in n for n in names)

    def test_extract_proc_names_regex(self):
        from src.ast.sql import extract_proc_names_regex
        names = extract_proc_names_regex(self.src)
        assert any("proc_GetProductById" in n for n in names)
        assert any("proc_InsertOrder" in n for n in names)
        assert any("proc_UpdateProduct" in n for n in names)

    def test_extract_column_info(self):
        from src.ast.sql import extract_column_info
        col_names, col_types = extract_column_info(self.root, self.src)
        assert "ProductId" in col_names
        assert "ProductName" in col_names

    def test_object_name_helper(self):
        from src.ast.sql import _object_name, _full_object_name
        from src.ast.cs import _find_all
        refs = _find_all(self.root, lambda n: n.type == "object_reference")
        refs_found = [r for r in refs if _object_name(r, self.src) == "Products"]
        assert len(refs_found) > 0

    def test_extract_referenced_tables(self):
        from src.ast.sql import extract_referenced_tables
        names = extract_referenced_tables(self.root, self.src)
        assert any("Products" in n for n in names)
