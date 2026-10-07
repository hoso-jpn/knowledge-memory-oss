"""Regressions for points raised in review of the Notion import and sync changes.

Every fixture here is synthetic. The File object shapes follow the documented Notion
reference: a hosted file carries an expiring url plus expiry_time, a file_upload carries
an id, and an external file carries a permanent url.
"""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from knowledge_memory.core import ID, MemoryError, build_index, search_notes
from knowledge_memory.notion_blocks import blocks_to_markdown
from knowledge_memory.notion_import import build, import_snapshot, resolve_body
from knowledge_memory.notion_semantics import stable_record
from knowledge_memory.sync import identifier, source_key, sync

# Synthetic Notion identifiers: one per role, in the same sort order as before.
DS = "00000000000000000000000000000004"
DB = "00000000000000000000000000000003"
PAGE = "00000000000000000000000000000002"
OTHER = "00000000000000000000000000000001"
CONFIG = {"version": 1, "body_source": "page", "properties": {
    "title": "Title", "domain": "分野", "source_type": "種別", "editorial_status": "整理状態",
    "source_description": "出典", "reference_url": "出典URL", "document_date": "資料日",
    "original_filename": "元ファイル"}}
SCHEMA = {"Title": "title", "分野": "multi_select", "種別": "select", "整理状態": "select",
          "出典": "rich_text", "出典URL": "url", "資料日": "date", "元ファイル": "rich_text"}


def text(value):
    return {"type": "text", "text": {"content": value, "link": None}, "plain_text": value,
            "href": None, "annotations": {"bold": False, "italic": False, "code": False,
                                          "strikethrough": False, "underline": False}}


def record(page_id, title="架空の学習ノート", blocks=None, parent=DS):
    return {
        "page": {"object": "page", "id": page_id, "url": f"https://www.notion.so/{page_id}",
                 "created_time": "2026-02-01T00:00:00.000Z",
                 "last_edited_time": "2026-03-01T00:00:00.000Z",
                 "archived": False, "in_trash": False,
                 "parent": {"type": "data_source_id", "data_source_id": parent,
                            "database_id": DB},
                 "properties": {
                     "Title": {"id": "t", "type": "title", "title": [text(title)]},
                     "分野": {"id": "d", "type": "multi_select",
                              "multi_select": [{"name": "架空分野"}]},
                     "種別": {"id": "k", "type": "select", "select": {"name": "研修ノート"}},
                     "整理状態": {"id": "e", "type": "select", "select": None},
                     "出典": {"id": "s", "type": "rich_text", "rich_text": [text("架空の出典")]},
                     "出典URL": {"id": "u", "type": "url", "url": "https://example.invalid/a"},
                     "資料日": {"id": "dd", "type": "date",
                                "date": {"start": "2026-02-20", "end": None, "time_zone": None}},
                     "元ファイル": {"id": "f", "type": "rich_text",
                                    "rich_text": [text("note.md")]}}},
        "properties_full": {},
        "blocks": blocks if blocks is not None else [
            {"id": "p1", "type": "paragraph", "has_children": False,
             "paragraph": {"rich_text": [text("架空の本文。")]}}],
    }


def snapshot(directory: Path, records, data_source=DS):
    directory.mkdir(parents=True, exist_ok=True)
    for item in records:
        (directory / f"{item['page']['id']}.json").write_text(
            json.dumps(item, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    (directory / "manifest.json").write_text(json.dumps(
        {"data_source_id": data_source, "database_id": DB, "complete": True,
         "records_saved": len(records), "schema_properties": SCHEMA,
         "fetch_finished_at": "2026-09-27T00:00:00+00:00"}, ensure_ascii=False), encoding="utf-8")
    return directory


HOSTED = lambda signature, expiry: {
    "type": "file", "file": {"url": "https://x.s3.amazonaws.com/a/note.md"
                                    f"?X-Amz-Signature={signature}&X-Amz-Expires=3600",
                             "expiry_time": expiry}}
UPLOAD = lambda value: {"type": "file_upload", "file_upload": {"id": value}}
EXTERNAL = lambda url: {"type": "external", "external": {"url": url}}


def attachment(payload, name="note.md"):
    """A file block wraps the File object one level below the block type."""
    return [{"id": "fb", "type": "file", "has_children": False,
             "file": {**payload, "caption": [text("保存時点の控え")], "name": name}}]


class FileObjectTests(unittest.TestCase):
    """Only the hosted shape is normalised; the other two must stay significant."""

    def outcome(self, first, second):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            snapshot(root / "snap", [record(PAGE, blocks=attachment(first))])
            import_snapshot(root / "snap", root / "vault", "knowledge", import_config=CONFIG)
            snapshot(root / "snap", [record(PAGE, blocks=attachment(second))])
            result = import_snapshot(root / "snap", root / "vault", "knowledge",
                                     import_config=CONFIG)
            return next(k for k in ("created", "revised", "unchanged") if result[k])

    def test_only_the_signed_url_and_expiry_are_ignored(self):
        self.assertEqual(self.outcome(HOSTED("aaaa", "2026-09-27T01:00:00.000Z"),
                                      HOSTED("bbbb", "2026-09-28T01:00:00.000Z")), "unchanged")

    def test_a_file_upload_id_change_is_a_real_change(self):
        # file_upload carries an id, not an expiring url, so nothing about it is ephemeral.
        self.assertEqual(self.outcome(UPLOAD("11111111-1111-1111-1111-111111111111"),
                                      UPLOAD("22222222-2222-2222-2222-222222222222")), "revised")

    def test_an_external_url_change_is_a_real_change(self):
        self.assertEqual(self.outcome(EXTERNAL("https://docs.invalid/a"),
                                      EXTERNAL("https://docs.invalid/b")), "revised")

    def test_swapping_the_payload_type_is_a_real_change(self):
        self.assertEqual(self.outcome(HOSTED("aaaa", "2026-09-27T01:00:00.000Z"),
                                      UPLOAD("11111111-1111-1111-1111-111111111111")), "revised")

    def test_normalisation_touches_neither_upload_nor_external(self):
        for payload in (UPLOAD("abc"), EXTERNAL("https://docs.invalid/a")):
            with self.subTest(payload=payload["type"]):
                self.assertEqual(stable_record(payload), payload)
        hosted = HOSTED("aaaa", "2026-09-27T01:00:00.000Z")
        stable = stable_record(hosted)
        self.assertNotIn("expiry_time", stable["file"])
        self.assertNotIn("X-Amz-Signature", stable["file"]["url"])
        self.assertIn("expiry_time", hosted["file"])      # the raw record is not mutated


class IdentifierTests(unittest.TestCase):
    """A data source selects pages; it is not a record."""

    def test_a_data_source_declaration_has_no_note_id(self):
        source = {"provider": "notion", "data_source_id": DS}
        self.assertEqual(source_key(source), "notion-data-source:" + DS)
        with self.assertRaisesRegex(MemoryError, "no note id"):
            identifier(source)

    def test_page_and_github_identifiers_are_unchanged_and_valid(self):
        page = identifier({"provider": "notion", "page_id": PAGE})
        self.assertEqual(page, "src-" + PAGE)
        repository = identifier({"provider": "github", "repo": "a/b", "path": "c.md",
                                 "ref": "main"})
        for value in (page, repository):
            with self.subTest(value=value):
                self.assertTrue(ID.fullmatch(value), f"{value!r} is not a usable note id")


class ConversionReportTests(unittest.TestCase):
    """The report describes the conversion whose body was stored."""

    def test_the_report_comes_from_the_stored_conversion(self):
        blocks = attachment(HOSTED("aaaa", "2026-09-27T01:00:00.000Z")) + [
            {"id": "toc", "type": "table_of_contents", "has_children": False,
             "table_of_contents": {}},
            {"id": "un", "type": "unsupported_widget", "has_children": False,
             "unsupported_widget": {"rich_text": [text("未対応の中身")]}}]
        report = []
        identifier_out, _, _, note = build(record(PAGE, blocks=blocks),
                                           {"fetch_finished_at": "2026-09-27T00:00:00+00:00",
                                            "schema_properties": SCHEMA, "data_source_id": DS},
                                           "knowledge", "page", None, None,
                                           import_config=CONFIG, report=report)
        actions = {entry["block_id"]: entry["action"] for entry in report}
        self.assertEqual(actions, {"fb": "attachment_reference_only",
                                   "toc": "omit_navigation", "un": "unsupported"})
        # Every block the report names is accounted for in the body it describes.
        self.assertIn("添付控え", note)
        self.assertIn("未対応ブロック: unsupported_widget", note)
        self.assertNotIn("X-Amz-Signature", note)

    def test_import_returns_one_report_per_stored_record(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            snapshot(root / "snap", [record(PAGE), record(OTHER, title="架空の二件目")])
            result = import_snapshot(root / "snap", root / "vault", "knowledge",
                                     import_config=CONFIG)
            self.assertEqual(sorted(result["conversion_reports"]), sorted(result["created"]))


class AdoptedBodyReportTests(unittest.TestCase):
    """The report describes the body that was adopted, not the one that was passed over."""

    UNUSED = [{"id": "un", "type": "unsupported_widget", "has_children": False,
               "unsupported_widget": {"rich_text": [text("採用されないページ本文")]}}]

    def record(self, slug="post-one", body="採用されたプロパティ本文"):
        page = {"id": PAGE, "url": "https://www.notion.so/" + PAGE,
                "properties": {
                    "Content": {"id": "c", "type": "rich_text", "rich_text": [text(body)]},
                    "Slug": {"id": "s", "type": "rich_text", "rich_text": [text(slug)]}}}
        full = {name: {"object": "list",
                       "results": [{"type": "rich_text", "rich_text": text(value)}]}
                for name, value in (("Content", body), ("Slug", slug))}
        return {"page": page, "properties_full": full, "blocks": list(self.UNUSED)}

    MANIFEST = {"schema_properties": {"Content": "rich_text", "Slug": "rich_text"}}

    def resolve(self, **kwargs):
        report = []
        body, where, _ = resolve_body(self.record(), self.MANIFEST,
                                      kwargs.pop("body_source", "auto"),
                                      kwargs.pop("body_property", "Content"),
                                      kwargs.pop("page_body_slugs", None),
                                      kwargs.pop("slug_property", "Slug"), report)
        return body, where, report

    def test_an_explicit_property_body_reports_nothing_about_the_page(self):
        body, where, report = self.resolve(body_source="property")
        self.assertEqual(where, "property:Content")
        self.assertIn("採用されたプロパティ本文", body)
        self.assertEqual(report, [])

    def test_the_slug_rule_choosing_the_property_reports_nothing_about_the_page(self):
        body, where, report = self.resolve(page_body_slugs=["another-slug"])
        self.assertEqual(where, "property:Content")
        self.assertEqual(report, [])

    def test_the_slug_rule_choosing_the_page_body_reports_it(self):
        body, where, report = self.resolve(page_body_slugs=["post-one"])
        self.assertEqual(where, "page_body")
        self.assertEqual([entry["block_id"] for entry in report], ["un"])
        self.assertIn("未対応ブロック: unsupported_widget", body)

    def test_an_explicit_page_body_reports_it(self):
        body, where, report = self.resolve(body_source="page")
        self.assertEqual(where, "page_body")
        self.assertEqual([entry["block_id"] for entry in report], ["un"])

    def test_the_report_never_names_a_block_absent_from_the_stored_body(self):
        for kwargs in ({"body_source": "property"}, {"page_body_slugs": ["another-slug"]},
                       {"body_source": "page"}, {"page_body_slugs": ["post-one"]}):
            with self.subTest(kwargs=kwargs):
                body, _, report = self.resolve(**dict(kwargs))
                for entry in report:
                    self.assertIn(entry["block_id"], body)


class ConversionFailureTests(unittest.TestCase):
    """A rejected conversion leaves nothing half-written."""

    BAD = [{"id": "t", "type": "table", "has_children": True,
            "table": {"table_width": 3, "has_column_header": False, "has_row_header": False},
            "_children": [{"id": "r", "type": "table_row", "has_children": False,
                           "table_row": {"cells": [[text("a")], [text("b")]]}}]}]

    def test_a_malformed_table_is_refused_rather_than_repaired(self):
        with self.assertRaises(MemoryError):
            blocks_to_markdown(self.BAD)

    def test_import_writes_nothing_when_one_record_fails(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            snapshot(root / "snap", [record(PAGE), record(OTHER, blocks=self.BAD)])
            with self.assertRaises(MemoryError):
                import_snapshot(root / "snap", root / "vault", "knowledge",
                                import_config=CONFIG)
            self.assertEqual(list((root / "vault").rglob("*.md")), [])

    def test_import_leaves_an_existing_vault_untouched(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            snapshot(root / "snap", [record(PAGE), record(OTHER)])
            import_snapshot(root / "snap", root / "vault", "knowledge", import_config=CONFIG)
            target = root / "vault" / "20-sources"
            before = {p.name: p.read_bytes() for p in target.glob("*.md")}
            snapshot(root / "snap", [record(PAGE, title="架空の更新"),
                                     record(OTHER, blocks=self.BAD)])
            with self.assertRaises(MemoryError):
                import_snapshot(root / "snap", root / "vault", "knowledge",
                                import_config=CONFIG)
            self.assertEqual(before, {p.name: p.read_bytes() for p in target.glob("*.md")})
            self.assertEqual(list(target.glob(".km-*")), [])

    def test_sync_holds_only_the_failing_page_for_review(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            configuration = root / "cfg"
            configuration.mkdir()
            (configuration / "import.yaml").write_text(
                yaml.safe_dump(CONFIG, allow_unicode=True), encoding="utf-8")
            (configuration / "sync.yaml").write_text(yaml.safe_dump(
                {"scope": "knowledge", "selection_id": "knowledge",
                 "sources": [{"provider": "notion", "data_source_id": DS,
                              "import_config": "import.yaml"}]}, allow_unicode=True),
                encoding="utf-8")
            good, bad = record(PAGE), record(OTHER, blocks=self.BAD)
            vault, state = root / "vault", root / "state"
            result = sync(configuration / "sync.yaml", vault, state,
                          fetch=_Transport([good, bad]))
            statuses = {item["source"]: item["status"] for item in result["sources"]}
            self.assertEqual(statuses["notion:" + PAGE], "created")
            self.assertEqual(statuses["notion:" + OTHER], "needs_review")
            self.assertFalse(result["complete"])
            stored = sorted(p.stem for p in (vault / "20-sources").glob("*.md"))
            self.assertEqual(stored, ["src-" + PAGE])
            database = root / "index.sqlite3"
            build_index(vault, database)
            found = search_notes(database, "架空の本文", "knowledge", include_candidates=True)
            self.assertEqual([hit["id"] for hit in found], ["src-" + PAGE])


class _Transport:
    """Serves the documented Notion shapes for the records it is given."""

    def __init__(self, records):
        self.records = {item["page"]["id"]: item for item in records}
        self.children = {}
        for item in records:
            self._index(item["blocks"])

    def _index(self, blocks):
        for block in blocks or []:
            if block.get("_children"):
                self.children[block["id"]] = [self._flatten(c) for c in block["_children"]]
                self._index(block["_children"])

    @staticmethod
    def _flatten(block):
        flat = {k: v for k, v in block.items() if k != "_children"}
        if block.get("_children"):
            flat["has_children"] = True
        return flat

    def __call__(self, provider, path, method="GET", body=None):
        import re
        if path.startswith("/data_sources/") and path.endswith("/query"):
            return {"object": "list", "has_more": False, "next_cursor": None,
                    "results": [item["page"] for item in self.records.values()]}
        if re.fullmatch(r"/data_sources/[0-9a-f-]+", path):
            return {"object": "data_source", "id": DS,
                    "parent": {"type": "database_id", "database_id": DB}, "properties": {}}
        found = re.fullmatch(r"/pages/([0-9a-f-]+)", path)
        if found:
            return self.records[found.group(1)]["page"]
        found = re.fullmatch(r"/blocks/([^/]+)/children\?.*", path)
        if found:
            key = found.group(1)
            if key in self.records:
                return {"object": "list", "has_more": False, "next_cursor": None,
                        "results": [self._flatten(b) for b in self.records[key]["blocks"]]}
            return {"object": "list", "has_more": False, "next_cursor": None,
                    "results": self.children.get(key, [])}
        found = re.fullmatch(r"/pages/([0-9a-f-]+)/properties/([^?]+)\?.*", path)
        if found:
            page = self.records[found.group(1)]["page"]["properties"]
            for value in page.values():
                if value.get("id") == found.group(2):
                    kind = value["type"]
                    return {"object": "list", "has_more": False, "next_cursor": None,
                            "results": [{"type": kind, kind: v}
                                        for v in (value.get(kind) or [])]}
            return {"object": "list", "has_more": False, "next_cursor": None, "results": []}
        raise AssertionError("unexpected path " + path)


if __name__ == "__main__":
    unittest.main()
