"""Keep application imports independent of the command-line launchers."""

import ast
import subprocess
import sys
import tempfile
import unittest

from pawception.paths import ROOT


class EntrypointTests(unittest.TestCase):
    def test_application_does_not_import_scripts(self):
        for source in (ROOT / "pawception").glob("*.py"):
            with self.subTest(source=source.name):
                for node in ast.walk(ast.parse(source.read_text())):
                    modules = []
                    if isinstance(node, ast.Import):
                        modules = [alias.name for alias in node.names]
                    elif isinstance(node, ast.ImportFrom):
                        modules = [node.module or ""]
                    self.assertFalse(any(name.split(".")[0] == "scripts" for name in modules))

    def test_launchers_delegate_to_package_main(self):
        for source in (ROOT / "scripts").glob("*.py"):
            if source.name == "__init__.py":
                continue
            with self.subTest(source=source.name):
                tree = ast.parse(source.read_text())
                self.assertFalse(any(isinstance(node, (ast.FunctionDef, ast.ClassDef))
                                     for node in ast.walk(tree)))
                targets = [node.module for node in tree.body if isinstance(node, ast.ImportFrom)
                           and any(alias.name == "main" for alias in node.names)]
                self.assertEqual(len(targets), 1)
                self.assertTrue(targets[0].startswith("pawception."))
                implementation = ROOT.joinpath(*targets[0].split(".")).with_suffix(".py")
                self.assertTrue(any(isinstance(node, ast.FunctionDef) and node.name == "main"
                                    for node in ast.parse(implementation.read_text()).body))

    def test_commands_work_as_modules_and_direct_files(self):
        with tempfile.TemporaryDirectory() as other_directory:
            for name in ("replay_ui", "run_pipeline", "prepare_test_videos"):
                for direct in (False, True):
                    with self.subTest(command=name, direct=direct):
                        arguments = [str(ROOT / "scripts" / f"{name}.py")] if direct else ["-m", f"scripts.{name}"]
                        result = subprocess.run([sys.executable, *arguments, "--help"],
                                                cwd=other_directory if direct else ROOT,
                                                capture_output=True, text=True, timeout=20)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn("usage:", result.stdout)
