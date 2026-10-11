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


def snapshot(path):
    return {p.relative_to(path).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns)
            for p in path.rglob('*') if p.is_file()}


@unittest.skipUnless(DEMO.is_file(), 'the demo ships with the source checkout, not the sdist')
class DemoTests(unittest.TestCase):
    def run_demo(self, *args):
        with tempfile.TemporaryDirectory() as cwd:
            env = {k: v for k, v in os.environ.items() if k != 'NOTION_TOKEN'}
            return subprocess.run([sys.executable, str(DEMO), *args], cwd=cwd, env=env, capture_output=True,
                                  text=True, encoding='utf-8', timeout=300)

    def test_demo_runs_end_to_end_and_cleans_up(self):
        before = snapshot(ROOT / 'examples'), sorted(p.name for p in ROOT.iterdir())
        result = self.run_demo(*(() if HAS_MCP else ('--skip-mcp',)))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('DEMO OK', result.stdout)
        for step in ('km validate', 'km index', 'km search', 'km get', 'km context'):
            self.assertIn(f'$ {step} ', result.stdout)
        if HAS_MCP:
            self.assertIn("tools/list: ['build_context', 'get_memory', 'search_memory']", result.stdout)
            self.assertIn('refused: True', result.stdout)
        else:
            self.assertIn('SKIPPED', result.stdout)
        workspace = Path(re.search(r'^removed workspace: (.+)$', result.stdout, re.M).group(1))
        self.assertFalse(workspace.exists())
        self.assertFalse(workspace.is_relative_to(ROOT))
        self.assertEqual((snapshot(ROOT / 'examples'), sorted(p.name for p in ROOT.iterdir())), before)

    @unittest.skipIf(HAS_MCP, 'only meaningful without the mcp extra')
    def test_missing_mcp_extra_is_not_reported_as_success(self):
        result = self.run_demo()
        self.assertEqual(result.returncode, 2)
        self.assertIn('[mcp]', result.stderr)
        self.assertNotIn('DEMO OK', result.stdout)


if __name__ == '__main__':
    unittest.main()
