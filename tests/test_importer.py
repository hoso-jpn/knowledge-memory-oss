import csv
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from knowledge_memory.core import MemoryError, build_index, get_note, load_vault, search_notes
from knowledge_memory.evaluation import load_questions, run_evaluation
from knowledge_memory.html_text import html_to_markdown, looks_like_html
from knowledge_memory.importer import import_csv
from knowledge_memory.staging import render_digest

FIELDS = ["Name", "URL", "Date", "Published", "Status", "Body"]
ROWS = [
    {"Name": "架空例：表現型の計測", "URL": "https://www.notion.so/x/" + "a" * 32,
     "Date": "2025-04-12", "Published": "Yes", "Status": "Done",
     "Body": "<h2>背景</h2><p>草丈を計測した。</p>"
             "<blockquote><p>同一時刻に行う。</p><p>曇天は分ける。</p></blockquote>"
             "<pre><code>x = 1\n    y = 2\n</code></pre>"
             "<p>詳細は<a href=\"/posts/bbb\">別記事</a>、式は "
             "<span class=\"katex\"><span class=\"katex-mathml\"><math><semantics>"
             "<annotation encoding=\"application/x-tex\">E = mc^2</annotation>"
             "</semantics></math></span><span class=\"katex-html\">Emc2</span></span> 。"},
    {"Name": "架空例：平文の記録", "URL": "https://www.notion.so/x/" + "b" * 32,
     "Date": "2025-08-30", "Published": "No", "Status": "Draft",
     "Body": "一行目。\n二行目。\n\n段落。"},
]


def write_csv(path, rows, fields=FIELDS):
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    writer.writerows([{k: row[k] for k in fields if k in row} for row in rows])
    path.write_text(stream.getvalue(), encoding="utf-8-sig")
    return path


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.db = self.root / "index.sqlite3"
        self.csv = write_csv(self.root / "export.csv", ROWS)
        self.first = import_csv(self.csv, self.vault, "demo")

    def notes(self):
        return sorted(p.name for p in (self.vault / "20-sources").glob("*.md"))

    def fingerprint(self):
        return {p.name: p.read_bytes() for p in (self.vault / "20-sources").glob("*.md")}

    def test_rows_become_candidate_source_reports(self):
        self.assertEqual(len(self.first["created"]), 2)
        notes = {n.metadata["id"]: n for n in load_vault(self.vault)}
        for note in notes.values():
            self.assertEqual(note.metadata["status"], "candidate")
            self.assertEqual(note.metadata["claim_kind"], "source_report")
            self.assertNotIn("verified_at", note.metadata)
            self.assertEqual(note.metadata["origin"]["kind"], "notion_csv_export")

    def test_published_is_kept_without_being_treated_as_checked(self):
        notes = {n.metadata["id"]: n for n in load_vault(self.vault)}
        published = notes["src-" + "a" * 32]
        self.assertEqual(published.metadata["origin"]["published"], "Yes")
        self.assertEqual(published.metadata["origin"]["status_label"], "Done")
        # Published and Done must not turn into active or verified.
        self.assertEqual(published.metadata["status"], "candidate")
        self.assertNotIn("verified_at", published.metadata)
        self.assertEqual(notes["src-" + "b" * 32].metadata["origin"]["published"], "No")

    def test_retrieved_at_and_record_date_are_separate(self):
        note = {n.metadata["id"]: n for n in load_vault(self.vault)}["src-" + "a" * 32]
        self.assertEqual(note.metadata["event_at"], "2025-04-12")
        self.assertTrue(note.metadata["origin"]["retrieved_at"].startswith("20"))
        self.assertNotEqual(note.metadata["origin"]["retrieved_at"][:10],
                            note.metadata["event_at"])

    def test_source_hashes_identify_the_file_and_the_row(self):
        note = {n.metadata["id"]: n for n in load_vault(self.vault)}["src-" + "a" * 32]
        origin = note.metadata["origin"]
        self.assertEqual(origin["source_file"], "export.csv")
        self.assertEqual(origin["source_file_sha256"], self.first["csv_sha256"])
        self.assertEqual(len(origin["content_sha256"]), 64)
        self.assertNotEqual(origin["content_sha256"], origin["source_file_sha256"])

    def test_markup_and_japanese_survive_the_conversion(self):
        body = (self.vault / "20-sources" / f"src-{'a' * 32}.md").read_text(encoding="utf-8")
        for fragment in ("## 背景", "> 同一時刻に行う。", ">\n> 曇天は分ける。", "```",
                         "    y = 2", "[別記事](/posts/bbb)", "$E = mc^2$", "草丈を計測した。"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, body)
        self.assertNotIn("Emc2", body)      # the duplicated KaTeX rendering is dropped
        plain = (self.vault / "20-sources" / f"src-{'b' * 32}.md").read_text(encoding="utf-8")
        self.assertIn("一行目。\n二行目。\n\n段落。", plain)

    def test_second_import_changes_nothing(self):
        before = self.fingerprint()
        again = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(again["created"], [])
        self.assertEqual(again["revised"], [])
        self.assertEqual(len(again["unchanged"]), 2)
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(len(self.notes()), 2)

    def test_a_changed_row_is_revised_in_place(self):
        rows = [dict(row) for row in ROWS]
        rows[0]["Body"] += "<p>追記。</p>"
        write_csv(self.csv, rows)
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["revised"], ["src-" + "a" * 32])
        self.assertEqual(result["unchanged"], ["src-" + "b" * 32])
        self.assertEqual(len(self.notes()), 2)
        self.assertIn("追記。", (self.vault / "20-sources" / f"src-{'a' * 32}.md").read_text())

    def test_an_invalid_row_writes_nothing(self):
        before = self.fingerprint()
        rows = [dict(row) for row in ROWS]
        rows[0]["Body"] += "<p>先頭行も変わった。</p>"      # would otherwise be rewritten
        rows[1]["Name"] = ""                                # but this row is unusable
        write_csv(self.csv, rows)
        with self.assertRaisesRegex(MemoryError, "row 3"):
            import_csv(self.csv, self.vault, "demo")
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(list((self.vault / "20-sources").glob(".km-import-*")), [])

    def test_bad_input_is_refused(self):
        cases = {
            "duplicate": ([dict(ROWS[0]), dict(ROWS[0])], "duplicate record id"),
            "empty": ([], "no rows"),
        }
        for name, (rows, message) in cases.items():
            with self.subTest(case=name):
                write_csv(self.csv, rows)
                with self.assertRaisesRegex(MemoryError, message):
                    import_csv(self.csv, self.vault, "demo")
        write_csv(self.csv, ROWS, fields=["Name", "URL"])
        with self.assertRaisesRegex(MemoryError, "no body column"):
            import_csv(self.csv, self.vault, "demo")
        write_csv(self.csv, ROWS)
        with self.assertRaisesRegex(MemoryError, "scope"):
            import_csv(self.csv, self.vault, "Demo")
        with self.assertRaisesRegex(MemoryError, "subdir"):
            import_csv(self.csv, self.vault, "demo", subdir="a/b")
        with self.assertRaisesRegex(MemoryError, "outside the vault"):
            import_csv(self.csv, self.vault, "demo", raw_dir=self.vault / "raw")

    def test_dry_run_leaves_the_vault_alone(self):
        rows = [dict(row) for row in ROWS]
        rows[0]["Body"] += "<p>追記。</p>"
        write_csv(self.csv, rows)
        before = self.fingerprint()
        result = import_csv(self.csv, self.vault, "demo", dry_run=True)
        self.assertEqual(result["revised"], ["src-" + "a" * 32])
        self.assertEqual(before, self.fingerprint())

    def test_missing_id_column_falls_back_to_a_stable_title_hash(self):
        write_csv(self.csv, ROWS, fields=["Name", "Date", "Published", "Status", "Body"])
        vault = self.root / "vault2"
        vault.mkdir()
        first = import_csv(self.csv, vault, "demo")
        self.assertEqual(first["record_id_from"], ["title hash"])
        again = import_csv(self.csv, vault, "demo")
        self.assertEqual(again["created"], [])
        self.assertEqual(sorted(again["unchanged"]), sorted(first["created"]))

    def test_imported_notes_are_searchable_only_when_candidates_are_included(self):
        build_index(self.vault, self.db)
        self.assertEqual(search_notes(self.db, "草丈", "demo"), [])
        found = search_notes(self.db, "草丈", "demo", include_candidates=True)
        self.assertEqual([r["id"] for r in found], ["src-" + "a" * 32])
        note = get_note(self.db, "src-" + "a" * 32, "demo", include_candidates=True)
        self.assertIn("$E = mc^2$", note["content"])
        self.assertEqual(note["metadata"]["origin"]["published"], "Yes")

    def test_rows_dropped_from_the_csv_are_reported_not_deleted(self):
        write_csv(self.csv, [ROWS[0]])
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["absent_from_csv"], ["src-" + "b" * 32])
        self.assertEqual(len(self.notes()), 2)


class SchemaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.vault = Path(self.temp.name) / "vault"
        self.vault.mkdir()

    def write(self, origin, **overrides):
        meta = dict(id="src-x", title="t", kind="source", status="candidate", scope="demo",
                    claim_kind="source_report", updated="2026-09-21",
                    sources=[{"ref": "fixture:1", "locator": "a"}], origin=origin)
        meta.update(overrides)
        (self.vault / "x.md").write_text(
            "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n本文",
            encoding="utf-8")

    def origin(self, **overrides):
        base = dict(kind="notion_csv_export", record_id="r1", source_file="export.csv",
                    source_file_sha256="a" * 64, content_sha256="b" * 64,
                    retrieved_at="2026-09-21T00:00:00+00:00")
        base.update(overrides)
        return base

    def test_valid_origin_is_accepted(self):
        self.write(self.origin(published="Yes", status_label="Done"))
        self.assertEqual(len(load_vault(self.vault)), 1)

    def test_broken_origin_is_rejected(self):
        cases = [
            self.origin(source_file_sha256="nope"), self.origin(content_sha256="B" * 64),
            self.origin(source_file="dir/export.csv"),
            self.origin(retrieved_at="2026-09-21T00:00:00"),
            self.origin(record_id=""), {"kind": "notion_csv_export"},
        ]
        for origin in cases:
            with self.subTest(origin=sorted(origin)):
                self.write(origin)
                with self.assertRaises(MemoryError):
                    load_vault(self.vault)
        self.write(self.origin(), claim_kind="user_statement")
        with self.assertRaisesRegex(MemoryError, "source_report"):
            load_vault(self.vault)


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.db = self.root / "index.sqlite3"
        import_csv(write_csv(self.root / "export.csv", ROWS), self.vault, "demo")
        build_index(self.vault, self.db)
        self.questions = self.root / "q.yaml"

    def save(self, questions):
        self.questions.write_text(yaml.safe_dump({"questions": questions}, allow_unicode=True),
                                  encoding="utf-8")

    def test_hits_misses_and_false_hits_are_counted(self):
        self.save([
            {"id": "q1", "query": "草丈", "expect_ids": ["src-" + "a" * 32]},
            {"id": "q2", "query": "二行目。", "expect_ids": ["src-" + "b" * 32]},
            {"id": "q3", "query": "kubernetes", "expect": "unknown"},
            {"id": "q4", "query": "草丈", "expect": "unknown"},
        ])
        report = run_evaluation(self.db, self.questions, "demo", include_candidates=True)
        self.assertEqual((report["hit"], report["miss"], report["false_hit"]), (2, 0, 1))
        # Without opting in, candidate snapshots are simply not measured.
        default = run_evaluation(self.db, self.questions, "demo")
        self.assertEqual((default["hit"], default["miss"], default["false_hit"]), (0, 2, 0))

    def test_malformed_question_sets_are_refused(self):
        for questions in ([{"id": "q1"}], [{"id": "q1", "query": "x", "expect": "maybe"}],
                          [{"id": "q1", "query": "x"}],
                          [{"id": "q1", "query": "x", "expect": "unknown",
                            "expect_ids": ["a"]}],
                          [{"id": "q1", "query": "x", "expect_ids": ["a"]},
                           {"id": "q1", "query": "y", "expect_ids": ["b"]}]):
            with self.subTest(questions=questions):
                self.save(questions)
                with self.assertRaises(MemoryError):
                    load_questions(self.questions)

    def test_shipped_question_set_matches_the_sample_corpus(self):
        repository = Path(__file__).resolve().parent.parent
        sample = repository / "examples" / "notion-csv" / "blog-sample.csv"
        questions = repository / "evaluation" / "blog-sample-questions.yaml"
        if not sample.exists() or not questions.exists():
            self.skipTest("sample corpus is not present")
        vault = self.root / "sample-vault"
        vault.mkdir()
        db = self.root / "sample.sqlite3"
        import_csv(sample, vault, "demo")
        build_index(vault, db)
        report = run_evaluation(db, questions, "demo", include_candidates=True)
        self.assertEqual(report["questions"], 20)
        self.assertEqual((report["miss"], report["false_hit"]), (0, 0))


class CliTests(unittest.TestCase):
    def test_import_reimport_index_search_get(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            vault = root / "vault"
            vault.mkdir()
            export = write_csv(root / "export.csv", ROWS)
            db = root / "index.sqlite3"

            def km(*args):
                result = subprocess.run([sys.executable, "-m", "knowledge_memory", *args],
                                        capture_output=True, text=True)
                return result.returncode, result.stdout, result.stderr

            code, out, _ = km("import-csv", "--csv", str(export), "--vault", str(vault),
                              "--scope", "demo")
            self.assertEqual(code, 0)
            self.assertEqual(len(json.loads(out)["created"]), 2)
            code, out, _ = km("import-csv", "--csv", str(export), "--vault", str(vault),
                              "--scope", "demo")
            self.assertEqual((code, json.loads(out)["created"]), (0, []))
            self.assertEqual(km("index", "--vault", str(vault), "--db", str(db))[0], 0)
            code, out, _ = km("search", "--db", str(db), "--scope", "demo",
                              "--include-candidates", "草丈")
            self.assertEqual(json.loads(out)[0]["id"], "src-" + "a" * 32)
            code, out, _ = km("get", "--db", str(db), "--scope", "demo",
                              "--include-candidates", "src-" + "a" * 32)
            self.assertIn("$E = mc^2$", json.loads(out)["content"])
            code, _, err = km("import-csv", "--csv", str(root / "missing.csv"),
                              "--vault", str(vault), "--scope", "demo")
            self.assertEqual(code, 2)
            self.assertIn("error", json.loads(err))


class HtmlTests(unittest.TestCase):
    def test_structure_is_kept_and_characters_are_not_rewritten(self):
        cases = {
            "<h3>題</h3>": "### 題\n",
            "<p>a<br>b</p>": "a\nb\n",
            "<ul><li>甲</li><li>乙</li></ul>": "- 甲\n- 乙\n",
            "<ol><li>甲</li></ol>": "1. 甲\n",
            "<p>*強調* _下線_ $x^2$ \\alpha</p>": "*強調* _下線_ $x^2$ \\alpha\n",
            "<p>A &amp; B</p>": "A & B\n",
            "<script>bad()</script><p>後</p>": "後\n",
            "plain\ntext": "plain\ntext\n",
            "": "",
        }
        for source, expected in cases.items():
            with self.subTest(source=source):
                self.assertEqual(html_to_markdown(source), expected)


if __name__ == "__main__":
    unittest.main()


class TransactionTests(unittest.TestCase):
    """A failure part way through a write must leave the tree exactly as it was."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.rows = [dict(ROWS[0], Name=f"記事{i}", URL=f"https://n.so/{chr(97 + i) * 32}",
                          Body=f"<p>本文{i}</p>") for i in range(3)]
        self.csv = write_csv(self.root / "export.csv", self.rows)
        import_csv(self.csv, self.vault, "demo")

    def fingerprint(self):
        return {p.name: p.read_bytes() for p in (self.vault / "20-sources").iterdir()}

    def leftovers(self):
        return sorted(p.name for p in (self.vault / "20-sources").glob(".km-*"))

    def test_a_failure_between_writes_rolls_every_file_back(self):
        before = self.fingerprint()
        for row in self.rows:
            row["Body"] += "<p>追記。</p>"
        write_csv(self.csv, self.rows)
        real, calls = os.replace, [0]

        def flaky(source, destination):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("simulated failure part way through")
            return real(source, destination)

        with mock.patch("knowledge_memory.staging.os.replace", flaky):
            with self.assertRaisesRegex(OSError, "part way through"):
                import_csv(self.csv, self.vault, "demo")
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.leftovers(), [])

    def test_a_failure_removes_files_this_run_created(self):
        before = self.fingerprint()
        self.rows[0]["Body"] += "<p>既存も変わった。</p>"
        self.rows.append(dict(ROWS[0], Name="新記事", URL="https://n.so/" + "z" * 32,
                              Body="<p>新しい本文</p>"))
        write_csv(self.csv, self.rows)
        real, calls = os.replace, [0]

        def flaky(source, destination):
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("simulated failure after the first create")
            return real(source, destination)

        with mock.patch("knowledge_memory.staging.os.replace", flaky):
            with self.assertRaises(OSError):
                import_csv(self.csv, self.vault, "demo")
        self.assertEqual(before, self.fingerprint())
        self.assertFalse((self.vault / "20-sources" / f"src-{'z' * 32}.md").exists())
        self.assertEqual(self.leftovers(), [])

    def test_a_failure_while_staging_writes_nothing(self):
        before = self.fingerprint()
        for row in self.rows:
            row["Body"] += "<p>追記。</p>"
        write_csv(self.csv, self.rows)
        with mock.patch("knowledge_memory.staging.shutil.copy2",
                        side_effect=OSError("no space left")):
            with self.assertRaises(OSError):
                import_csv(self.csv, self.vault, "demo")
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.leftovers(), [])


class ExistingNoteTests(unittest.TestCase):
    """Re-import must not quietly discard edits, approvals or a different scope."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.csv = write_csv(self.root / "export.csv", ROWS)
        import_csv(self.csv, self.vault, "demo")
        self.note = self.vault / "20-sources" / f"src-{'a' * 32}.md"

    def change_source(self):
        rows = [dict(row) for row in ROWS]
        rows[0]["Body"] += "<p>上流で変わった。</p>"
        write_csv(self.csv, rows)

    def test_a_hand_edited_note_is_reported_and_kept(self):
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\n手で足した一行。\n",
                             encoding="utf-8")
        before = self.note.read_bytes()
        self.change_source()
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["locally_modified"], ["src-" + "a" * 32])
        self.assertEqual(result["revised"], [])
        self.assertEqual(before, self.note.read_bytes())

    def test_a_promoted_note_is_reported_and_kept(self):
        self.note.write_text(self.note.read_text(encoding="utf-8")
                             .replace("status: candidate", "status: active"), encoding="utf-8")
        # Re-stamp so the note still looks untouched apart from the promotion.
        text = self.note.read_text(encoding="utf-8")
        import re as _re
        blanked = _re.sub(r"render_sha256: [0-9a-f]{64}", "render_sha256: __render__", text)
        self.note.write_text(
            blanked.replace("render_sha256: __render__",
                            f"render_sha256: {render_digest(blanked)}", 1), encoding="utf-8")
        before = self.note.read_bytes()
        self.change_source()
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["promoted"], ["src-" + "a" * 32])
        self.assertEqual(result["revised"], [])
        self.assertEqual(before, self.note.read_bytes())

    def test_importing_into_a_different_scope_is_refused(self):
        with self.assertRaisesRegex(MemoryError, "different scope"):
            import_csv(self.csv, self.vault, "other")
        self.assertIn("scope: demo", self.note.read_text(encoding="utf-8"))


class FormatDetectionTests(unittest.TestCase):
    """Markdown must survive untouched even when it carries an inline tag."""

    def test_markdown_with_an_inline_tag_is_not_treated_as_html(self):
        source = ("## 見出し\n\n> 引用された一行。\n> 続きの行。\n\n"
                  "詳細は <a href=\"https://example.com\">リンク</a> を参照。\n\n"
                  "- 箇条書き\n- 二つ目\n\n```python\nx = 1\n```\n\n$E = mc^2$\n")
        self.assertFalse(looks_like_html(source))
        self.assertEqual(html_to_markdown(source), source)

    def test_real_html_is_still_converted(self):
        for source in ("<h2>題</h2><p>本文</p>", "<ul><li>甲</li></ul>",
                       "<blockquote><p>引用</p></blockquote>"):
            with self.subTest(source=source):
                self.assertTrue(looks_like_html(source))
        self.assertEqual(html_to_markdown("<h2>題</h2><p>本文</p>"), "## 題\n\n本文\n")

    def test_a_bare_inline_tag_alone_is_plain_text(self):
        for source in ("見出し<br>次の行", "<https://example.com> を参照", "型は <tb> と書く"):
            with self.subTest(source=source):
                self.assertFalse(looks_like_html(source))


class RenderDigestTests(unittest.TestCase):
    """The digest must ignore per-read frontmatter values and nothing else."""

    FRONT = ("---\nid: src-x\ntitle: t\nkind: source\nstatus: candidate\nscope: demo\n"
             "claim_kind: source_report\nupdated: '2026-09-21'\nsources:\n"
             "- ref: notion:x\n  locator: l\norigin:\n  kind: notion_api\n  record_id: x\n"
             "  content_sha256: " + "a" * 64 + "\n"
             "  retrieved_at: '2026-09-21T00:00:00+00:00'\n"
             "  render_sha256: " + "b" * 64 + "\n---\n")

    def note(self, body):
        return self.FRONT + body

    def test_a_body_line_named_like_a_metadata_key_still_counts(self):
        for before, after in (("updated: old setting", "updated: corrected setting"),
                              ("retrieved_at: never", "retrieved_at: always"),
                              ("  source_file_sha256: x", "  source_file_sha256: y"),
                              ("render_sha256: none", "render_sha256: some")):
            with self.subTest(line=before):
                a = self.note(f"# t\n\n```yaml\n{before}\n```\n")
                b = self.note(f"# t\n\n```yaml\n{after}\n```\n")
                self.assertNotEqual(render_digest(a), render_digest(b))

    def test_re_reading_the_same_record_does_not_move_the_digest(self):
        a = self.note("# t\n\n本文。\n")
        b = (a.replace("retrieved_at: '2026-09-21T00:00:00+00:00'",
                       "retrieved_at: '2026-09-30T12:00:00+00:00'")
              .replace("updated: '2026-09-21'", "updated: '2026-09-30'"))
        self.assertEqual(render_digest(a), render_digest(b))

    def test_frontmatter_edits_still_count(self):
        a = self.note("# t\n\n本文。\n")
        for changed in ("title: t", "status: candidate", "  record_id: x"):
            with self.subTest(changed=changed):
                b = a.replace(changed, changed + "2")
                self.assertNotEqual(render_digest(a), render_digest(b))


class LegacyStampTests(unittest.TestCase):
    """Notes written before the digest was scoped to the frontmatter."""

    # How the digest used to be computed: the whole note, at any indent.
    LEGACY = re.compile(r"^(\s*)(render_sha256|retrieved_at|updated|source_file_sha256): .*$",
                        re.M)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.csv = write_csv(self.root / "export.csv", ROWS)
        import_csv(self.csv, self.vault, "demo")
        self.note = self.vault / "20-sources" / f"src-{'a' * 32}.md"

    def legacy_digest(self, text):
        import hashlib
        return hashlib.sha256(self.LEGACY.sub(r"\1\2: -", text).encode()).hexdigest()

    def restamp_as_legacy(self):
        text = self.note.read_text(encoding="utf-8")
        blanked = re.sub(r"render_sha256: [0-9a-f]{64}", "render_sha256: " + "0" * 64, text)
        self.note.write_text(
            blanked.replace("render_sha256: " + "0" * 64,
                            f"render_sha256: {self.legacy_digest(blanked)}", 1),
            encoding="utf-8")

    def change_source(self):
        rows = [dict(row) for row in ROWS]
        rows[0]["Body"] += "<p>上流で変わった。</p>"
        write_csv(self.csv, rows)

    def test_an_ordinary_old_note_is_not_disturbed(self):
        # Without a metadata-shaped body line the two digests agree, so nothing changes.
        before = self.note.read_bytes()
        self.restamp_as_legacy()
        self.assertEqual(before, self.note.read_bytes())
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["locally_modified"], [])
        self.assertIn("src-" + "a" * 32, result["unchanged"])

    def test_an_old_note_the_old_stamp_could_not_cover_is_protected(self):
        text = self.note.read_text(encoding="utf-8")
        self.note.write_text(text + "\n```yaml\nupdated: corrected setting\n```\n",
                             encoding="utf-8")
        self.restamp_as_legacy()          # stamped the way the old code would have
        before = self.note.read_bytes()
        self.change_source()
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["locally_modified"], ["src-" + "a" * 32])
        self.assertEqual(result["revised"], [])
        self.assertEqual(before, self.note.read_bytes())

    def test_an_old_note_edited_by_hand_is_still_protected(self):
        self.restamp_as_legacy()
        self.note.write_text(self.note.read_text(encoding="utf-8") + "\n手で足した行。\n",
                             encoding="utf-8")
        before = self.note.read_bytes()
        self.change_source()
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["locally_modified"], ["src-" + "a" * 32])
        self.assertEqual(before, self.note.read_bytes())

    def test_a_hand_edit_to_a_body_line_named_like_a_key_is_protected(self):
        text = self.note.read_text(encoding="utf-8")
        self.note.write_text(text + "\n```yaml\nupdated: corrected setting\n```\n",
                             encoding="utf-8")
        before = self.note.read_bytes()
        self.change_source()
        result = import_csv(self.csv, self.vault, "demo")
        self.assertEqual(result["locally_modified"], ["src-" + "a" * 32])
        self.assertEqual(result["revised"], [])
        self.assertEqual(before, self.note.read_bytes())
        self.assertIn("updated: corrected setting",
                      self.note.read_text(encoding="utf-8"))
