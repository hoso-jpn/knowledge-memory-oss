import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from knowledge_memory.cli import json_for_stream
from knowledge_memory.core import MemoryError, build_index, get_note, load_vault, search_notes


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.db = self.root / "index.sqlite3"
        self.write("current", "育種では遺伝子型を観測する。50%の例。", aliases=["elastic net"])
        self.write("old", "育種の旧判断。", status="superseded")
        self.write("draft", "育種の未確認仮説。", status="candidate")
        self.write("withdrawn", "育種の撤回された記録。", status="retracted")
        self.write("separate", "育種の別スコープ。", scope="separate")
        build_index(self.vault, self.db)

    def write(self, identifier, body="sample", **overrides):
        metadata = dict(id=identifier, title=identifier, kind="note", status="active",
                        scope="personal", claim_kind="source_report", updated="2026-09-21",
                        sources=[{"ref": "fixture:run-001", "locator": "summary"}])
        metadata.update(overrides)
        path = self.vault / f"{identifier}.md"
        path.write_text("---\n" + yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False)
                        + "---\n" + body, encoding="utf-8")
        return path

    def test_japanese_two_character_query(self):
        self.assertEqual([r["id"] for r in search_notes(self.db, "育種", "personal")], ["current"])

    def test_japanese_trigram_query(self):
        self.assertEqual(search_notes(self.db, "遺伝子型", "personal")[0]["id"], "current")

    def test_alias_and_unicode_normalization(self):
        self.assertEqual(search_notes(self.db, "ＥＬＡＳＴＩＣ　ＮＥＴ", "personal")[0]["id"], "current")

    def test_literal_query_does_not_expand_wildcards_or_boolean_syntax(self):
        self.assertEqual(len(search_notes(self.db, "%", "personal")), 1)
        self.assertEqual(search_notes(self.db, "_", "personal"), [])
        self.assertEqual(search_notes(self.db, '育種 OR separate', "personal"), [])
        self.assertEqual(search_notes(self.db, '" OR "', "personal"), [])

    def test_control_characters_do_not_truncate_the_index(self):
        self.write("nul", "育種の前半\x00表現型の後半")
        build_index(self.vault, self.db)
        self.assertEqual(search_notes(self.db, "表現型の後半", "personal")[0]["id"], "nul")
        self.assertEqual(search_notes(self.db, "育種の前半\x00表現型の後半", "personal")[0]["id"], "nul")
        with self.assertRaisesRegex(MemoryError, "empty after normalization"):
            search_notes(self.db, "\x00\x00\x00", "personal")
        self.assertEqual(get_note(self.db, "nul", "personal")["content"].count("\x00"), 1)

    def test_index_from_an_older_normalization_is_rejected(self):
        import sqlite3
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE meta SET value = '1' WHERE key = 'schema_version'")
        with self.assertRaisesRegex(MemoryError, "rebuild the index"):
            search_notes(self.db, "育種", "personal")
        with self.assertRaisesRegex(MemoryError, "rebuild the index"):
            get_note(self.db, "current", "personal")

    def test_candidate_and_history_are_explicit(self):
        self.assertEqual({r["id"] for r in search_notes(self.db, "育種", "personal", include_candidates=True)},
                         {"current", "draft"})
        self.assertEqual({r["id"] for r in search_notes(self.db, "育種", "personal", include_history=True)},
                         {"current", "old", "withdrawn"})

    def test_get_respects_scope_and_status(self):
        for identifier in ("separate", "old", "draft", "withdrawn", "../../etc/passwd"):
            with self.subTest(identifier=identifier), self.assertRaises(MemoryError):
                get_note(self.db, identifier, "personal")
        self.assertEqual(get_note(self.db, "old", "personal", include_history=True)["id"], "old")

    def test_returned_evidence_matches_source_bytes(self):
        result = get_note(self.db, "current", "personal")
        source = self.vault / result["path"]
        self.assertEqual(result["content"], source.read_text())
        self.assertEqual(result["sha256"], hashlib.sha256(source.read_bytes()).hexdigest())
        self.assertEqual(result["metadata"]["sources"][0]["ref"], "fixture:run-001")

    def test_changed_file_cannot_be_served_as_current(self):
        path = self.vault / "current.md"
        path.write_text(path.read_text() + "changed")
        with self.assertRaisesRegex(MemoryError, "changed"):
            search_notes(self.db, "育種", "personal")

    def test_deleted_file_is_removed_after_reindex(self):
        (self.vault / "current.md").unlink()
        with self.assertRaisesRegex(MemoryError, "changed"):
            get_note(self.db, "current", "personal")
        self.assertEqual(build_index(self.vault, self.db), 4)
        self.assertEqual(search_notes(self.db, "育種", "personal"), [])

    def test_new_note_requires_reindex(self):
        self.write("new")
        with self.assertRaisesRegex(MemoryError, "changed"):
            search_notes(self.db, "育種", "personal")

    def test_duplicate_ids_do_not_replace_existing_index(self):
        before = self.db.read_bytes()
        (self.vault / "duplicate.md").write_bytes((self.vault / "current.md").read_bytes())
        with self.assertRaisesRegex(MemoryError, "duplicate id"):
            build_index(self.vault, self.db)
        self.assertEqual(before, self.db.read_bytes())

    def test_invalid_metadata_and_duplicate_keys(self):
        cases = [
            {"status": "confirmed"}, {"updated": "yesterday"}, {"updated": None},
            {"sources": []}, {"unexpected": "value"}, {"aliases": "not a list"},
            {"updated": "2026-09-21T12:00:00"},
        ]
        for case in cases:
            with self.subTest(case=case):
                self.write("current", **case)
                with self.assertRaises(MemoryError):
                    load_vault(self.vault)
        p = self.write("current")
        p.write_text(p.read_text().replace("title: current", "title: current\ntitle: duplicate"))
        with self.assertRaisesRegex(MemoryError, "unique"):
            load_vault(self.vault)

    def test_invalid_links_and_supersession(self):
        for extra in ({"related_ids": ["missing"]}, {"related_ids": ["separate"]},
                      {"supersedes": ["draft"]}, {"related_ids": ["current"]}):
            with self.subTest(extra=extra):
                self.write("current", **extra)
                with self.assertRaises(MemoryError):
                    load_vault(self.vault)
        self.write("current", supersedes=["old"])
        self.assertEqual(len(load_vault(self.vault)), 5)

    def test_symlinked_sources_are_rejected(self):
        outside = self.root / "outside.md"
        outside.write_text("outside")
        (self.vault / "external.md").symlink_to(outside)
        with self.assertRaisesRegex(MemoryError, "symlinks"):
            load_vault(self.vault)

    def test_index_path_cannot_replace_a_source(self):
        with self.assertRaises(MemoryError):
            build_index(self.vault, self.vault / "index.sqlite3")
        with self.assertRaises(MemoryError):
            build_index(self.vault, self.root / "notes.md")

    def test_input_limits_and_required_scope(self):
        for query, scope, limit in [("", "personal", 10), ("x", "", 10), ("x", "personal", 0),
                                    ("x", "personal", 51), ("x" * 513, "personal", 10)]:
            with self.subTest(query=query, scope=scope, limit=limit), self.assertRaises(MemoryError):
                search_notes(self.db, query, scope, limit)

    def test_cli_json_falls_back_to_ascii_escapes_for_legacy_stream_encoding(self):
        class Stream:
            encoding = "cp1252"

        value = {"query": "育種"}
        text = json_for_stream(value, Stream())
        self.assertNotIn("育種", text)
        self.assertEqual(json.loads(text), value)

    def test_cli_json_and_nonzero_error(self):
        command = [sys.executable, "-m", "knowledge_memory", "search", "--db", str(self.db),
                   "--scope", "personal", "育種"]
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        self.assertEqual(json.loads(result.stdout)[0]["id"], "current")
        self.assertEqual(result.stderr, "")
        self.write("new")
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("changed", json.loads(result.stderr)["error"])


if __name__ == "__main__":
    unittest.main()
