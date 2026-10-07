"""Notion fetch and import, driven by a local stand-in for the API.

No test reaches the network. The stand-in returns the shapes the real API documents,
including cursor pagination, nested blocks and a truncated rich_text property.
"""

import json
import threading
import unittest
import unittest.mock
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlparse, parse_qs

from knowledge_memory.core import MemoryError, build_index, get_note, load_vault, search_notes
from knowledge_memory.notion_api import NotionError, fetch_snapshot
from knowledge_memory.notion_blocks import blocks_to_markdown, property_text
from knowledge_memory.notion_import import describe, import_snapshot, load_snapshot

TOKEN = "secret-test-token"
DATA_SOURCE = "00000000000000000000000000000003"  # synthetic; distinct from the page IDs below


def text(content, **annotations):
    return {"type": "text", "text": {"content": content, "link": None},
            "plain_text": content, "href": None,
            "annotations": {"code": False, **annotations}}


def page(identifier, title, date, published, tags, label="分類-001", slug="post"):
    return {
        "object": "page", "id": identifier,
        "url": f"https://www.notion.so/{identifier.replace('-', '')}",
        "last_edited_time": "2026-03-04T05:06:00.000Z",
        "archived": False, "in_trash": False,
        "properties": {
            "名前": {"id": "title", "type": "title", "title": [text(label)]},
            "Title": {"id": "titl", "type": "rich_text", "rich_text": [text(title)]},
            "Slug": {"id": "slug", "type": "rich_text", "rich_text": [text(slug)]},
            "Description": {"id": "desc", "type": "rich_text", "rich_text": []},
            "Date": {"id": "date", "type": "date", "date": {"start": date, "end": None}},
            "Published": {"id": "pub", "type": "checkbox", "checkbox": published},
            "Tags": {"id": "tags", "type": "multi_select",
                     "multi_select": [{"name": t} for t in tags]},
            "Content": {"id": "cont", "type": "rich_text", "rich_text": [text("(truncated)")]},
        },
    }


PAGES = [
    page("11111111-1111-1111-1111-111111111111", "架空例：圃場の表現型計測", "2025-04-12",
         True, ["agri", "phenotyping"], "分類-001", "post-one"),
    page("22222222-2222-2222-2222-222222222222", "架空例：露出の決め方", "2025-05-03",
         False, ["imaging"], "分類-002", "post-two"),
]

BLOCKS = {
    "11111111-1111-1111-1111-111111111111": [
        {"id": "b1", "type": "heading_2", "has_children": False,
         "heading_2": {"rich_text": [text("背景")]}},
        {"id": "b2", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [text("草丈と葉色を週次で計測した。")]}},
        {"id": "b3", "type": "quote", "has_children": False,
         "quote": {"rich_text": [text("計測は同一時刻に行うこと。")]}},
        {"id": "b4", "type": "code", "has_children": False,
         "code": {"language": "python", "rich_text": [text("x = 1\n    y = 2")]}},
        {"id": "b5", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [
             text("式は "),
             {"type": "equation", "equation": {"expression": "E = mc^2"},
              "plain_text": "E = mc^2", "annotations": {"code": False}},
             text(" 。詳細は"),
             {"type": "text", "text": {"content": "別記事", "link": {"url": "/posts/abc"}},
              "plain_text": "別記事", "href": "/posts/abc",
              "annotations": {"code": False}},
             text("。")]}},
        {"id": "b6", "type": "bulleted_list_item", "has_children": True,
         "bulleted_list_item": {"rich_text": [text("親項目")]}},
    ],
    "22222222-2222-2222-2222-222222222222": [
        {"id": "c1", "type": "paragraph", "has_children": False,
         "paragraph": {"rich_text": [text("露出時間は照度に反比例させる。")]}},
    ],
    "b6": [{"id": "b6a", "type": "bulleted_list_item", "has_children": False,
            "bulleted_list_item": {"rich_text": [text("子項目")]}}],
}

# Properties read back in full. The query response above showed "(truncated)".
FULL_CONTENT = {
    "11111111-1111-1111-1111-111111111111":
        ["Contentプロパティ側の本文。", "続きの二個目の断片。"],
    "22222222-2222-2222-2222-222222222222": [],
}
FULL_TITLE = {p["id"]: [p["properties"]["Title"]["rich_text"][0]["plain_text"]] for p in PAGES}


class FakeNotion(BaseHTTPRequestHandler):
    paginate_pages = True
    stall_cursor = False
    break_property = False

    def log_message(self, *args):
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self):
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send({"message": "unauthorized"}, 401)
            return False
        if not self.headers.get("Notion-Version"):
            self._send({"message": "missing version"}, 400)
            return False
        return True

    def do_GET(self):
        if not self._authorized():
            return
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        parts = parsed.path.strip("/").split("/")
        if parts[:1] == ["data_sources"]:
            self._send({"object": "data_source", "id": DATA_SOURCE, "properties": {
                "名前": {"id": "title", "type": "title"},
                "Title": {"id": "titl", "type": "rich_text"},
                "Slug": {"id": "slug", "type": "rich_text"},
                "Description": {"id": "desc", "type": "rich_text"},
                "Content": {"id": "cont", "type": "rich_text"},
                "Date": {"id": "date", "type": "date"},
                "Published": {"id": "pub", "type": "checkbox"},
                "Tags": {"id": "tags", "type": "multi_select"}}})
        elif parts[:1] == ["blocks"]:
            self._send({"object": "list", "results": BLOCKS.get(parts[1], []),
                        "has_more": False, "next_cursor": None})
        elif parts[:1] == ["pages"] and parts[2:3] == ["properties"]:
            if parts[3] == "title":
                page_object = next(p for p in PAGES if p["id"] == parts[1])
                self._send({"object": "list", "has_more": False, "next_cursor": None,
                            "results": [{"type": "title",
                                         "title": page_object["properties"]["名前"]["title"][0]}]})
                return
            if parts[3] in ("slug", "desc"):
                page_object = next(p for p in PAGES if p["id"] == parts[1])
                items = page_object["properties"]["Slug" if parts[3] == "slug"
                                                  else "Description"]["rich_text"]
                self._send({"object": "list", "has_more": False, "next_cursor": None,
                            "results": [{"type": "rich_text", "rich_text": i} for i in items]})
                return
            if parts[3] == "titl":
                self._send({"object": "list", "has_more": False, "next_cursor": None,
                            "results": [{"type": "rich_text", "rich_text": text(piece)}
                                        for piece in FULL_TITLE.get(parts[1], [])]})
                return
            fragments = FULL_CONTENT.get(parts[1], [])
            if type(self).break_property:
                # Claims there is more to read but hands back no cursor to read it with.
                self._send({"object": "list", "results": [], "has_more": True,
                            "next_cursor": None})
                return
            cursor = query.get("start_cursor", [None])[0]
            index = int(cursor) if cursor else 0
            if index < len(fragments) - 1:
                self._send({"object": "list", "has_more": True, "next_cursor": str(index + 1),
                            "results": [{"type": "rich_text",
                                         "rich_text": text(fragments[index])}]})
            else:
                results = ([{"type": "rich_text", "rich_text": text(fragments[index])}]
                           if fragments else [])
                self._send({"object": "list", "has_more": False, "next_cursor": None,
                            "results": results})
        else:
            self._send({"message": "not found"}, 404)

    def do_POST(self):
        if not self._authorized():
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")
        if not self.path.startswith(f"/data_sources/{DATA_SOURCE}/query"):
            self._send({"message": "not found"}, 404)
            return
        if not type(self).paginate_pages:
            self._send({"object": "list", "results": PAGES, "has_more": False,
                        "next_cursor": None})
            return
        cursor = body.get("start_cursor")
        if type(self).stall_cursor:
            self._send({"object": "list", "results": PAGES[:1], "has_more": True,
                        "next_cursor": "stuck"})
        elif cursor is None:
            self._send({"object": "list", "results": PAGES[:1], "has_more": True,
                        "next_cursor": "c1"})
        else:
            self._send({"object": "list", "results": PAGES[1:], "has_more": False,
                        "next_cursor": None})


class NotionTestCase(unittest.TestCase):
    def setUp(self):
        FakeNotion.paginate_pages = True
        FakeNotion.stall_cursor = False
        FakeNotion.break_property = False
        self.server = HTTPServer(("127.0.0.1", 0), FakeNotion)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.snapshot = self.root / "snapshot"
        self.vault = self.root / "vault"
        self.vault.mkdir()
        self.db = self.root / "index.sqlite3"

    def fetch(self, **kwargs):
        return fetch_snapshot(TOKEN, self.snapshot, data_source=DATA_SOURCE,
                              base=self.base, **kwargs)


class FetchTests(NotionTestCase):
    def test_every_page_and_nested_block_is_collected(self):
        manifest = self.fetch()
        self.assertEqual((manifest["pages_returned"], manifest["records_saved"]), (2, 2))
        self.assertTrue(manifest["complete"])
        self.assertEqual(manifest["schema_properties"]["Content"], "rich_text")
        _, records = load_snapshot(self.snapshot)
        first = next(r for r in records if r["page"]["id"] == PAGES[0]["id"])
        nested = [b for b in first["blocks"] if b["id"] == "b6"][0]
        self.assertEqual(nested["_children"][0]["id"], "b6a")

    def test_truncated_property_is_read_back_in_full(self):
        self.fetch()
        _, records = load_snapshot(self.snapshot)
        first = next(r for r in records if r["page"]["id"] == PAGES[0]["id"])
        full = property_text(first["properties_full"]["Content"])
        self.assertEqual(full, "Contentプロパティ側の本文。続きの二個目の断片。")
        self.assertNotIn("(truncated)", full)

    def test_a_cursor_that_does_not_advance_is_a_failed_fetch(self):
        FakeNotion.stall_cursor = True
        with self.assertRaisesRegex(NotionError, "did not advance"):
            self.fetch()
        self.assertFalse(self.snapshot.exists())

    def test_a_failed_fetch_leaves_the_previous_snapshot_intact(self):
        self.fetch()
        before = {p.name: p.read_bytes() for p in self.snapshot.iterdir()}
        FakeNotion.stall_cursor = True
        with self.assertRaises(NotionError):
            self.fetch()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.snapshot.iterdir()})

    def test_a_bad_token_is_reported_without_writing(self):
        with self.assertRaisesRegex(NotionError, "refused the request"):
            fetch_snapshot("wrong", self.snapshot, data_source=DATA_SOURCE, base=self.base)
        self.assertFalse(self.snapshot.exists())
        with self.assertRaisesRegex(NotionError, "no Notion token"):
            fetch_snapshot("", self.snapshot, data_source=DATA_SOURCE, base=self.base)

    def test_arguments_are_checked(self):
        with self.assertRaisesRegex(NotionError, "exactly one"):
            fetch_snapshot(TOKEN, self.snapshot, base=self.base)
        with self.assertRaisesRegex(NotionError, "exactly one"):
            fetch_snapshot(TOKEN, self.snapshot, data_source="a", database="b", base=self.base)
        with self.assertRaisesRegex(NotionError, "outside the vault"):
            fetch_snapshot(TOKEN, self.vault / "snap", data_source=DATA_SOURCE,
                           base=self.base, vault=self.vault)


class ImportTests(NotionTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.fetch()

    def test_both_body_locations_are_reported_and_not_merged(self):
        report = describe(*load_snapshot(self.snapshot))
        self.assertEqual(report["records"], 2)
        self.assertEqual(report["with_page_body"], 2)
        self.assertEqual(report["with_property_text"]["Content"], 1)
        # Page 1 has text in both places, so auto must hold it back rather than guess.
        result = import_snapshot(self.snapshot, self.vault, "demo")
        self.assertEqual([h["id"] for h in result["needs_body_choice"]], ["1" * 32])
        self.assertIn("both the page body", result["needs_body_choice"][0]["reason"])
        self.assertNotIn("1" * 32, "".join(result["created"]))
        self.assertFalse((self.vault / "20-sources" / f"src-{'1' * 32}.md").exists())
        # Page 2 is unambiguous, so it is imported in the same run.
        self.assertEqual(result["created"], ["src-" + "2" * 32])

    def test_choosing_the_page_body(self):
        result = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(result["body_from"], {"page_body": 2})
        body = (self.vault / "20-sources" / f"src-{'1' * 32}.md").read_text(encoding="utf-8")
        for fragment in ("## 背景", "> 計測は同一時刻に行うこと。", "```python", "    y = 2",
                         "$E = mc^2$", "[別記事](/posts/abc)", "- 親項目", "  - 子項目"):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, body)
        self.assertNotIn("Contentプロパティ側の本文。", body)

    def test_choosing_the_content_property(self):
        result = import_snapshot(self.snapshot, self.vault, "demo", body_source="property")
        self.assertEqual(result["body_from"], {"property:Content": 2})
        body = (self.vault / "20-sources" / f"src-{'1' * 32}.md").read_text(encoding="utf-8")
        self.assertIn("Contentプロパティ側の本文。続きの二個目の断片。", body)
        self.assertNotIn("## 背景", body)

    def test_provenance_records_page_url_edit_time_and_fetch_time(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        note = {n.metadata["id"]: n for n in load_vault(self.vault)}["src-" + "1" * 32]
        origin = note.metadata["origin"]
        self.assertEqual(origin["kind"], "notion_api")
        self.assertEqual(origin["record_id"], "1" * 32)
        self.assertTrue(origin["source_url"].startswith("https://www.notion.so/"))
        self.assertEqual(origin["source_edited_at"], "2026-03-04T05:06:00.000Z")
        self.assertEqual(origin["retrieved_at"], self.manifest["fetch_finished_at"])
        self.assertNotEqual(origin["source_edited_at"], origin["retrieved_at"])
        self.assertEqual(len(origin["content_sha256"]), 64)
        self.assertEqual(origin["body_from"], "page_body")
        self.assertEqual(note.metadata["event_at"], "2025-04-12")

    def test_published_is_kept_without_becoming_active_or_verified(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        notes = {n.metadata["id"]: n for n in load_vault(self.vault)}
        self.assertEqual(notes["src-" + "1" * 32].metadata["origin"]["published"], "true")
        self.assertEqual(notes["src-" + "2" * 32].metadata["origin"]["published"], "false")
        for note in notes.values():
            self.assertEqual(note.metadata["status"], "candidate")
            self.assertEqual(note.metadata["claim_kind"], "source_report")
            self.assertNotIn("verified_at", note.metadata)

    def test_second_import_changes_nothing(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        before = {p.name: p.read_bytes() for p in (self.vault / "20-sources").iterdir()}
        again = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual((again["created"], again["revised"]), ([], []))
        self.assertEqual(len(again["unchanged"]), 2)
        self.assertEqual(before, {p.name: p.read_bytes()
                                  for p in (self.vault / "20-sources").iterdir()})

    def test_a_refetch_after_an_edit_revises_only_that_page(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        BLOCKS[PAGES[0]["id"]].append({"id": "b7", "type": "paragraph", "has_children": False,
                                       "paragraph": {"rich_text": [text("追記された段落。")]}})
        self.addCleanup(BLOCKS[PAGES[0]["id"]].pop)
        self.fetch()
        result = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual(result["revised"], ["src-" + "1" * 32])
        self.assertEqual(result["unchanged"], ["src-" + "2" * 32])
        self.assertEqual(len(list((self.vault / "20-sources").glob("*.md"))), 2)

    def test_an_incomplete_snapshot_is_refused(self):
        manifest = json.loads((self.snapshot / "manifest.json").read_text())
        manifest["complete"] = False
        (self.snapshot / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MemoryError, "incomplete"):
            import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        manifest["complete"] = True
        manifest["records_saved"] = 99
        (self.snapshot / "manifest.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(MemoryError, "manifest says"):
            import_snapshot(self.snapshot, self.vault, "demo", body_source="page")

    def test_dry_run_and_scope_are_checked(self):
        result = import_snapshot(self.snapshot, self.vault, "demo", body_source="page",
                                 dry_run=True)
        self.assertEqual(len(result["created"]), 2)
        self.assertFalse((self.vault / "20-sources").exists())
        with self.assertRaisesRegex(MemoryError, "scope"):
            import_snapshot(self.snapshot, self.vault, "Demo", body_source="page")

    def test_imported_pages_are_searchable_as_candidates(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        build_index(self.vault, self.db)
        self.assertEqual(search_notes(self.db, "草丈", "demo"), [])
        found = search_notes(self.db, "草丈", "demo", include_candidates=True)
        self.assertEqual([r["id"] for r in found], ["src-" + "1" * 32])
        note = get_note(self.db, "src-" + "1" * 32, "demo", include_candidates=True)
        self.assertIn("$E = mc^2$", note["content"])
        self.assertIn("notion.so", note["metadata"]["origin"]["source_url"])


class BlockTests(unittest.TestCase):
    def test_blocks_render_without_rewriting_characters(self):
        cases = [
            ([{"type": "heading_1", "heading_1": {"rich_text": [text("題")]}}], "# 題\n"),
            ([{"type": "to_do", "to_do": {"checked": True, "rich_text": [text("済")]}}],
             "- [x] 済\n"),
            ([{"type": "numbered_list_item",
               "numbered_list_item": {"rich_text": [text("甲")]}}], "1. 甲\n"),
            ([{"type": "equation", "equation": {"expression": "\\int_0^1 x dx"}}],
             "$$\\int_0^1 x dx$$\n"),
            ([{"type": "divider", "divider": {}}], "---\n"),
            ([{"type": "paragraph",
               "paragraph": {"rich_text": [text("a_b *c* \\alpha $x$")]}}],
             "a_b *c* \\alpha $x$\n"),
            ([{"type": "paragraph", "paragraph": {"rich_text": [
                text("code", code=True)]}}], "`code`\n"),
            ([], ""),
        ]
        for blocks, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(blocks_to_markdown(blocks), expected)


if __name__ == "__main__":
    unittest.main()


class SettingsChangeTests(NotionTestCase):
    """The source record and the way it was converted are two different identities."""

    def setUp(self):
        super().setUp()
        self.fetch()
        self.note = self.vault / "20-sources" / f"src-{'1' * 32}.md"

    def test_changing_the_body_source_regenerates_the_note(self):
        first = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual(len(first["created"]), 2)
        body = self.note.read_text(encoding="utf-8")
        self.assertIn("## 背景", body)

        # Same snapshot, different conversion settings: the record did not change, the
        # output must. Treating this as unchanged would keep the old body and title.
        second = import_snapshot(self.snapshot, self.vault, "demo", body_source="property",
                                 body_property="Content", title_property="Title")
        self.assertEqual(second["revised"], ["src-" + "1" * 32, "src-" + "2" * 32])
        self.assertEqual(second["unchanged"], [])
        rewritten = self.note.read_text(encoding="utf-8")
        self.assertIn("Contentプロパティ側の本文。", rewritten)
        self.assertNotIn("## 背景", rewritten)

    def test_the_source_hash_alone_would_not_have_noticed(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        before = load_vault(self.vault)[0].metadata["origin"]["content_sha256"]
        import_snapshot(self.snapshot, self.vault, "demo", body_source="property",
                        body_property="Content", title_property="Title")
        after = {n.metadata["id"]: n for n in load_vault(self.vault)}["src-" + "1" * 32]
        # The record is identical, so the record hash is too; only the render hash moved.
        self.assertEqual(after.metadata["origin"]["content_sha256"], before)
        self.assertIn("body=property", after.metadata["origin"]["render_settings"])

    def test_re_running_with_the_same_settings_is_still_a_no_op(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        before = {p.name: p.read_bytes() for p in (self.vault / "20-sources").iterdir()}
        again = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual(len(again["unchanged"]), 2)
        self.assertEqual(before, {p.name: p.read_bytes()
                                  for p in (self.vault / "20-sources").iterdir()})

    def test_a_later_fetch_of_unchanged_pages_is_a_no_op(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        before = {p.name: p.read_bytes() for p in (self.vault / "20-sources").iterdir()}
        self.fetch()          # a new read, so retrieved_at moves
        again = import_snapshot(self.snapshot, self.vault, "demo", body_source="page")
        self.assertEqual(len(again["unchanged"]), 2)
        self.assertEqual(before, {p.name: p.read_bytes()
                                  for p in (self.vault / "20-sources").iterdir()})


class IncompletePropertyTests(NotionTestCase):
    """A property page that claims more results but gives no cursor is not a complete read."""

    def test_inconsistent_property_pages_are_refused(self):
        from knowledge_memory import notion_api
        cases = {
            "no cursor": {"object": "list", "results": [], "has_more": True,
                          "next_cursor": None},
            "blank cursor": {"object": "list", "results": [], "has_more": True,
                             "next_cursor": ""},
            "cursor without more": {"object": "list", "results": [], "has_more": False,
                                    "next_cursor": "c1"},
            "results not a list": {"object": "list", "results": None, "has_more": False},
        }
        for name, response in cases.items():
            with self.subTest(case=name):
                with self.assertRaises(NotionError):
                    notion_api._full_property(lambda *a, **k: response, "page", "prop")

    def test_an_incomplete_property_leaves_the_previous_snapshot_intact(self):
        self.fetch()
        before = {p.name: p.read_bytes() for p in self.snapshot.iterdir()}
        original = FULL_CONTENT[PAGES[0]["id"]]
        FakeNotion.break_property = True
        self.addCleanup(lambda: setattr(FakeNotion, "break_property", False))
        with self.assertRaisesRegex(NotionError, "incomplete|inconsistent"):
            self.fetch()
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.snapshot.iterdir()})
        self.assertEqual(FULL_CONTENT[PAGES[0]["id"]], original)

    def test_a_failed_directory_swap_restores_the_previous_snapshot(self):
        from knowledge_memory import notion_api
        self.fetch()
        before = {p.name: p.read_bytes() for p in self.snapshot.iterdir()}
        real, calls = notion_api.os.replace, [0]

        def flaky(source, destination):
            calls[0] += 1
            if calls[0] == 2:      # the old snapshot has been moved aside by now
                raise OSError("simulated failure installing the new snapshot")
            return real(source, destination)

        with unittest.mock.patch.object(notion_api.os, "replace", flaky):
            with self.assertRaises(OSError):
                self.fetch()
        self.assertTrue(self.snapshot.is_dir())
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.snapshot.iterdir()})
        self.assertFalse(self.snapshot.with_name(self.snapshot.name + ".previous").exists())


class AmbiguousBodyTests(NotionTestCase):
    def test_an_explicit_choice_imports_the_held_back_record(self):
        self.fetch()
        auto = import_snapshot(self.snapshot, self.vault, "demo")
        self.assertEqual([h["id"] for h in auto["needs_body_choice"]], ["1" * 32])
        chosen = import_snapshot(self.snapshot, self.vault, "demo", body_source="property",
                                 body_property="Content", title_property="Title")
        self.assertEqual(chosen["needs_body_choice"], [])
        self.assertIn("src-" + "1" * 32, chosen["created"])
        self.assertTrue((self.vault / "20-sources" / f"src-{'1' * 32}.md").exists())


class SlugRuleTests(NotionTestCase):
    """An explicit per-slug list decides the body, as the publisher does."""

    def setUp(self):
        super().setUp()
        self.fetch()

    def test_the_listed_slug_uses_the_page_body(self):
        result = import_snapshot(self.snapshot, self.vault, "demo",
                                 body_property="Content", title_property="名前,Title",
                                 page_body_slugs=["post-one"])
        self.assertEqual(result["body_from"], {"page_body": 1})
        self.assertEqual(result["body_from_by_id"]["src-" + "1" * 32], "page_body")
        first = (self.vault / "20-sources" / f"src-{'1' * 32}.md").read_text(encoding="utf-8")
        self.assertIn("## 背景", first)
        self.assertIn("body_rule: slug post-one is in the page-body list", first)
        # post-two is not listed and has no Content, which is the case the publisher
        # refuses to build. It is held back with the reason rather than guessed at.
        held = result["needs_body_choice"]
        self.assertEqual([h["id"] for h in held], ["2" * 32])
        self.assertIn("not in the page-body list", held[0]["reason"])

    def test_a_listed_slug_with_an_empty_page_body_falls_back_to_the_property(self):
        record = self.snapshot / f"{PAGES[0]['id']}.json"
        data = json.loads(record.read_text(encoding="utf-8"))
        data["blocks"] = []                      # listed, but nothing in the page body
        record.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        result = import_snapshot(self.snapshot, self.vault, "demo",
                                 body_property="Content", page_body_slugs=["post-one"])
        self.assertEqual(result["body_from_by_id"]["src-" + "1" * 32], "property:Content")
        note = (self.vault / "20-sources" / f"src-{'1' * 32}.md").read_text(encoding="utf-8")
        self.assertIn("its page body is empty", note)

    def test_a_record_with_no_body_anywhere_is_held_back(self):
        result = import_snapshot(self.snapshot, self.vault, "demo",
                                 body_property="Description", page_body_slugs=[])
        held = {h["id"] for h in result["needs_body_choice"]}
        self.assertIn("2" * 32, held)       # no page body and no Description text
        self.assertFalse((self.vault / "20-sources" / f"src-{'2' * 32}.md").exists())

    def test_the_selection_is_stable_across_runs(self):
        first = import_snapshot(self.snapshot, self.vault, "demo", body_property="Content",
                                page_body_slugs=["post-one"])
        again = import_snapshot(self.snapshot, self.vault, "demo", body_property="Content",
                                page_body_slugs=["post-one"])
        self.assertEqual(first["body_from_by_id"], again["body_from_by_id"])
        self.assertEqual(len(again["unchanged"]), 1)
        self.assertEqual([h["id"] for h in first["needs_body_choice"]],
                         [h["id"] for h in again["needs_body_choice"]])

    def test_the_composite_title_joins_the_parts(self):
        import_snapshot(self.snapshot, self.vault, "demo", body_property="Content",
                        title_property="名前,Title", page_body_slugs=["post-one"])
        note = (self.vault / "20-sources" / f"src-{'1' * 32}.md").read_text(encoding="utf-8")
        self.assertIn("title: 分類-001；架空例：圃場の表現型計測", note)
