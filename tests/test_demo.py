"""The five-minute demo in examples/demo.py keeps working and leaves nothing behind."""
import importlib.util
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'examples/demo.py'
HAS_MCP = importlib.util.find_spec('mcp') is not None
# Bytecode caches, build metadata and the developer's venv change for reasons unrelated to the demo.
UNTRACKED = {'.git', '.venv', '__pycache__'}


def snapshot(root):
    """Every file under the checkout except caches, with size and mtime."""
    files = {}
    for directory, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in UNTRACKED and not d.endswith('.egg-info')]
        for name in names:
            path = Path(directory, name)
            stat = path.lstat()
            files[path.relative_to(root).as_posix()] = (stat.st_size, stat.st_mtime_ns)
    return files


@unittest.skipUnless(DEMO.is_file(), 'the demo ships with the source checkout, not the sdist')
class DemoTests(unittest.TestCase):
    def run_demo(self, *args):
        """Run the demo with a private cwd and TMPDIR; return the result and what was left in them."""
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items() if k != 'NOTION_TOKEN'}
            env['TMPDIR'] = tmp
            result = subprocess.run([sys.executable, str(DEMO), *args], cwd=cwd, env=env, capture_output=True,
                                    text=True, encoding='utf-8', timeout=300)
            return result, sorted(os.listdir(cwd)) + sorted(os.listdir(tmp))

    def test_demo_runs_end_to_end_and_cleans_up(self):
        before = snapshot(ROOT)
        result, left = self.run_demo(*(() if HAS_MCP else ('--skip-mcp',)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DEMO OK', result.stdout)
        for step in ('km validate', 'km index', 'km search', 'km get', 'km context'):
            self.assertIn(f'$ {step} ', result.stdout)
        self.assertIn('refused: vault changed after indexing', result.stdout)
        if HAS_MCP:
            self.assertIn("tools/list: ['build_context', 'get_memory', 'search_memory']", result.stdout)
            self.assertIn('refused: True', result.stdout)
        else:
            self.assertIn('SKIPPED', result.stdout)
        self.assertRegex(result.stdout, re.compile(r'^removed workspace: ', re.M))
        self.assertEqual(left, [], 'the demo left files in its working or temporary directory')
        self.assertEqual(snapshot(ROOT), before, 'the demo changed files in the checkout')

    @unittest.skipIf(HAS_MCP, 'only meaningful without the mcp extra')
    def test_missing_mcp_extra_is_not_reported_as_success(self):
        result, left = self.run_demo()
        self.assertEqual(result.returncode, 2)
        self.assertIn('[mcp]', result.stderr)
        self.assertNotIn('DEMO OK', result.stdout)
        self.assertEqual(left, [])


if __name__ == '__main__':
    unittest.main()
