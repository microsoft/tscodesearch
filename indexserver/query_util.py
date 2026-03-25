"""
Thin entry point for running src/query/dispatch.py from the indexserver's Python environment.

Adds the tscodesearch root to sys.path so that tree_sitter_c_sharp (installed
in the indexserver venv) and src.ast.cs are found, then delegates to src.query.main().

Usage (same as src/query/dispatch.py CLI):
    python query_util.py --methods path/to/File.cs
    python query_util.py --declarations Widget path/to/File.cs
"""

import os
import sys

# Add the tscodesearch root (parent of this file's directory) to sys.path.
# This makes query.py, ast_cs.py, and config.py importable.
_ts_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ts_root not in sys.path:
    sys.path.insert(0, _ts_root)

from src.query import main

if __name__ == "__main__":
    main()
