"""Keep the foundation, plugins and harnesses separately replaceable.

The foundation is proofguard/ and bend-spec/. These tests cover static Python
imports and JavaScript import specifiers; they do not follow dynamic imports.
"""

import ast
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
FOUNDATION = (ROOT / "proofguard", ROOT / "bend-spec")
PLUGINS, HARNESSES = ROOT / "plugins", ROOT / "integrations"
# A harness's tests and its qualification runner may reach into the foundation.
TEST_TOOLING = {"tests", "review.py"}
JS_IMPORT = re.compile(r"""(?:\bfrom\s+|\bimport\s*\(?\s*)['"]([^'"]+)['"]""")


def imported_roots(path: Path) -> set[str]:
    """Name each module a Python source file imports, as dotted paths."""
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
    return names


def js_targets(path: Path) -> set[Path]:
    """Resolve the relative modules a JavaScript file imports."""
    return {(path.parent / spec).resolve() for spec in JS_IMPORT.findall(path.read_text())
            if spec.startswith(".")}


def sources(directory: Path, pattern: str = "*.py") -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.rglob(pattern)
            if not {"node_modules", ".cache", "artifacts"} & set(path.parts)]


def inside(path: Path, directory: Path) -> bool:
    return path == directory or directory in path.parents


class BoundaryTests(unittest.TestCase):
    def test_foundation_imports_no_plugin_or_harness(self):
        for directory in FOUNDATION:
            for path in sources(directory):
                for name in imported_roots(path):
                    self.assertFalse(name.startswith(("plugins", "integrations")), (path, name))
            for path in sources(directory, "*.mjs"):
                for target in js_targets(path):
                    self.assertFalse(inside(target, PLUGINS) or inside(target, HARNESSES), (path, target))

    def test_foundation_source_names_no_plugin_or_harness(self):
        plugins = [path.name for path in PLUGINS.iterdir()] if PLUGINS.is_dir() else []
        harnesses = [path.name for path in HARNESSES.iterdir()] if HARNESSES.is_dir() else []
        for path in (*sources(ROOT / "proofguard"), *(ROOT / "proofguard/stdlib").iterdir()):
            words = set(path.read_text().lower().replace("_", " ").replace("/", " ").replace(".", " ").split())
            for name in (*plugins, *harnesses):
                self.assertNotIn(name.lower(), words, path)

    def test_plugins_do_not_import_harnesses(self):
        for path in sources(PLUGINS):
            for name in imported_roots(path):
                self.assertFalse(name.startswith("integrations"), (path, name))
        for path in sources(PLUGINS, "*.mjs"):
            for target in js_targets(path):
                self.assertFalse(inside(target, HARNESSES), (path, target))

    def test_harnesses_do_not_import_plugins(self):
        for path in sources(HARNESSES):
            for name in imported_roots(path):
                self.assertFalse(name.startswith("plugins"), (path, name))
        for path in sources(HARNESSES, "*.mjs"):
            for target in js_targets(path):
                self.assertFalse(inside(target, PLUGINS), (path, target))

    def test_harnesses_reach_the_foundation_only_through_the_session(self):
        for path in sources(HARNESSES):
            if TEST_TOOLING & set(path.relative_to(HARNESSES).parts):
                continue
            for name in imported_roots(path):
                if name.startswith(("proofguard", "bendspec")):
                    self.assertEqual(name, "proofguard.session", path)
        for path in sources(HARNESSES, "*.mjs"):
            for target in js_targets(path):
                self.assertFalse(any(inside(target, directory) for directory in FOUNDATION), (path, target))

    def test_js_import_scan_sees_relative_imports(self):
        targets = js_targets(HARNESSES / "pi/guest.mjs")
        self.assertIn((HARNESSES / "pi/config.mjs").resolve(), targets)


if __name__ == "__main__":
    unittest.main()
