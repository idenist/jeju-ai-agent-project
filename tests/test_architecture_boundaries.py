"""Stable module-boundary contracts; independent of absolute contributor paths."""
import ast
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def imports(module):
    for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"), filename=str(module))):
        if isinstance(node,ast.ImportFrom) and node.module:
            yield node.module
        elif isinstance(node,ast.Import):
            for alias in node.names:
                yield alias.name


class ArchitectureBoundaries(unittest.TestCase):
    def test_services_never_import_agent_or_streamlit_ui(self):
        for path in (ROOT/"services").glob("*.py"):
            for name in imports(path):
                with self.subTest(source=path.name, imported=name):
                    self.assertFalse(name=="agents" or name.startswith("agents.") or
                                     name=="ui" or name.startswith("ui."))

    def test_agent_never_imports_streamlit_ui(self):
        for path in (ROOT/"agents").glob("*.py"):
            for name in imports(path):
                with self.subTest(source=path.name, imported=name):
                    self.assertFalse(name=="ui" or name.startswith("ui."))

    def test_ui_main_has_no_back_edges(self):
        for name in ("chat", "panels", "context"):
            path=ROOT/"ui"/(name+".py")
            for imported in imports(path):
                with self.subTest(source=name, imported=imported):
                    self.assertNotEqual(imported, "ui.application")

    def test_entrypoint_is_thin(self):
        path=ROOT/"app.py"
        self.assertLessEqual(len(path.read_text(encoding="utf-8").splitlines()),20)

if __name__=="__main__": unittest.main()
