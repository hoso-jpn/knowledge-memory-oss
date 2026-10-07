"""Turn a Notion snapshot into candidate notes.

The snapshot is what Notion returned. The notes are derived from it and say so: which page,
which URL, when Notion last edited it, when we read it, and a hash of what we read.
"""

from __future__ import annotations

import json
from pathlib import Path

from .core import ID, MAX_NOTE_BYTES, MemoryError, parse_note
from .html_text import looks_like_html, html_to_markdown
from .notion_blocks import blocks_to_markdown, property_markdown, property_text
from .notion_semantics import stable_record
from .notion_config import mapped_metadata, settings, validate_config
from .staging import digest as _digest, finalize, plan, write_all

class BodyConflict(MemoryError):
    """The record has text in two places and no rule says which one is the body."""


ORIGIN_KIND = "notion_api"
RENDER_VERSION = "notion/3"
TITLE_JOIN = "；"
TITLE_TYPES = ("title",)
BODY_CANDIDATES = ("content", "body", "本文")


def load_snapshot(directory: Path) -> tuple[dict, list[dict]]:
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise MemoryError(f"{directory} has no manifest.json; run fetch-notion first")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        raise MemoryError("the snapshot is marked incomplete; re-run fetch-notion")
    records = [json.loads(p.read_text(encoding="utf-8"))
               for p in sorted(directory.glob("*.json")) if p.name != "manifest.json"]
    if len(records) != manifest.get("records_saved"):
        raise MemoryError(f"snapshot has {len(records)} records but the manifest says "
                          f"{manifest.get('records_saved')}; re-run fetch-notion")
    return manifest, records


def describe(manifest: dict, records: list[dict]) -> dict:
    """Report where text actually lives, so the body source is chosen from real data."""
    counts = {"page_body": 0, "property": {}}
    for record in records:
        if blocks_to_markdown(record.get("blocks")).strip():
            counts["page_body"] += 1
        for name, value in (record.get("properties_full") or {}).items():
            if property_text(value).strip():
                counts["property"][name] = counts["property"].get(name, 0) + 1
    return {"records": len(records), "schema_properties": manifest.get("schema_properties", {}),
            "with_page_body": counts["page_body"], "with_property_text": counts["property"]}


def _title(record, manifest, override):
    """A comma-separated override joins the parts the way the blog composes a title."""
    properties = record["page"].get("properties") or {}
    full = record.get("properties_full") or {}
    if override:
        parts = []
        for name in [n.strip() for n in override.split(",") if n.strip()]:
            if name not in properties:
                raise MemoryError(f"title property {name!r} is not on the page")
            parts.append(property_text(full.get(name) or properties[name]).strip())
        return TITLE_JOIN.join(part for part in parts if part)
    for name, kind in (manifest.get("schema_properties") or {}).items():
        if kind in TITLE_TYPES and name in properties:
            return property_text(full.get(name) or properties[name]).strip()
    for name, value in properties.items():
        if value.get("type") in TITLE_TYPES:
            return property_text(full.get(name) or value).strip()
    return ""


def _body_property_name(manifest, override):
    if override:
        return override
    names = {n.lower(): n for n, kind in (manifest.get("schema_properties") or {}).items()
             if kind == "rich_text"}
    return next((names[n] for n in BODY_CANDIDATES if n in names), None)


def record_slug(record, slug_property):
    value = (record.get("properties_full") or {}).get(slug_property) \
        or (record["page"].get("properties") or {}).get(slug_property)
    return property_text(value).strip()


def resolve_body(record, manifest, body_source, body_property, page_body_slugs=None,
                 slug_property="Slug", report=None):
    """Return (text, where, why). Never merges two bodies and never guesses between them.

    A caller that passes `report` receives the conversion notes for the body that was
    actually adopted. The page blocks are converted either way, because the page body has
    to be inspected to decide, but notes about blocks that were not used are dropped
    rather than handed back describing a body nobody stored.
    """
    page_notes = []
    page_text = blocks_to_markdown(record.get("blocks"), record["page"].get("url")
                                   or "https://www.notion.so/" + record["page"]["id"].replace("-", ""),
                                   page_notes)

    def chosen(text, where, why):
        if report is not None and where == "page_body":
            report.extend(page_notes)
        return text, where, why

    name = _body_property_name(manifest, body_property)
    # The property is rebuilt the way the publisher builds it, then converted only if it
    # really is HTML. Markdown carrying an inline tag is left exactly as it was.
    raw = property_markdown((record.get("properties_full") or {}).get(name)) if name else ""
    property_body = html_to_markdown(raw) if raw and looks_like_html(raw) else raw

    if page_body_slugs is not None:
        # Mirrors the blog: an explicit per-slug list decides, never "whichever has text".
        slug = record_slug(record, slug_property)
        if not slug:
            raise BodyConflict(f"page {record['page']['id']} has no {slug_property} value, "
                               f"so the page-body list cannot be applied")
        if slug in page_body_slugs:
            if page_text.strip():
                return chosen(page_text, "page_body", f"slug {slug} is in the page-body list")
            if property_body.strip():
                return chosen(property_body, f"property:{name}",
                              f"slug {slug} is listed but its page body is empty")
            raise BodyConflict(f"page {record['page']['id']} ({slug}) has no body in either place")
        if property_body.strip():
            return chosen(property_body, f"property:{name}",
                          f"slug {slug} is not in the page-body list")
        raise BodyConflict(f"page {record['page']['id']} ({slug}) is not in the page-body list "
                           f"and its {name} property is empty")

    if body_source == "page":
        return chosen(page_text, "page_body", "--body-source page")
    if body_source == "property":
        if not name:
            raise MemoryError("no rich_text body property found; pass --body-property")
        return chosen(property_body, f"property:{name}", "--body-source property")
    if page_text.strip() and property_body.strip():
        raise BodyConflict(
            f"page {record['page']['id']} has text in both the page body and the "
            f"{name!r} property. Compare them and choose --body-source page or "
            f"--body-source property; they are not merged automatically.")
    if property_body.strip():
        return chosen(property_body, f"property:{name}", "only the property has text")
    if page_text.strip():
        return chosen(page_text, "page_body", "only the page body has text")
    raise BodyConflict(f"page {record['page']['id']} has no body in either place")


def build(record, manifest, scope, body_source, body_property, title_property,
          page_body_slugs=None, slug_property="Slug", import_config=None, report=None):
    page = record["page"]
    identifier = (page.get("id") or "").replace("-", "").lower()
    if not identifier or not ID.fullmatch(f"src-{identifier}"):
        raise MemoryError(f"page id {page.get('id')!r} is not usable as a record id")
    mapped = None
    if import_config is not None:
        validate_config(import_config)
        if body_source not in ("auto", "page") or body_property or title_property or page_body_slugs is not None:
            raise MemoryError("import-config cannot be combined with body/title/slug overrides")
        body_source = "page"
        mapped = mapped_metadata(record, import_config)
    title = mapped[0] if mapped else _title(record, manifest, title_property)
    if not title:
        raise MemoryError(f"page {page.get('id')} has no title text")
    body, where, why = resolve_body(record, manifest, body_source, body_property,
                                    page_body_slugs, slug_property, report)
    properties = page.get("properties") or {}
    meta = {
        "id": f"src-{identifier}",
        "title": title,
        "kind": "source",
        "status": "candidate",
        "scope": scope,
        "claim_kind": "source_report",
        "updated": (manifest.get("fetch_finished_at") or "")[:10],
        "sources": [{"ref": f"notion:{identifier}",
                     "locator": page.get("url") or f"notion page {identifier}"}],
        "origin": {
            "kind": ORIGIN_KIND,
            "record_id": identifier,
            # Hash of the snapshot we read, not of the live page.
            "content_sha256": _digest(json.dumps(record, ensure_ascii=False,
                                                 sort_keys=True).encode()),
            "semantic_sha256": _digest(json.dumps(stable_record(record), ensure_ascii=False,
                                                  sort_keys=True).encode()),
            "retrieved_at": manifest.get("fetch_finished_at"),
            "body_from": where,
            "body_rule": why,
            # How the note was produced, so changing it counts as a change.
            "render_settings": f"{RENDER_VERSION} body={body_source} "
                               f"body_property={body_property or '-'} "
                               f"title={title_property or '-'} "
                               f"page_body_slugs={'set' if page_body_slugs is not None else '-'}",
        },
    }
    if page.get("url"):
        meta["origin"]["source_url"] = page["url"]
    if page.get("last_edited_time"):
        meta["origin"]["source_edited_at"] = page["last_edited_time"]
    if page.get("created_time"):
        meta["origin"]["source_created_at"] = page["created_time"]
    parent = page.get("parent") or {}
    for key in ("database_id", "data_source_id"):
        declared, actual = manifest.get(key), parent.get(key)
        if declared and actual and declared.replace("-", "") != actual.replace("-", ""):
            raise MemoryError(f"page {identifier} has a different {key} from the snapshot")
        if actual or declared:
            meta["origin"][key] = actual or declared
    if mapped:
        meta["domain"] = mapped[1]
        meta["origin"]["source_metadata"] = mapped[2]
        meta["origin"]["render_settings"] += " " + settings(import_config)
        if mapped[2]["reference_url"]:
            meta["sources"].append({"ref": mapped[2]["reference_url"],
                                    "locator": "原本に記載された出典URL（未検証）"})
    for name, value in ({} if mapped else properties).items():
        kind = value.get("type")
        text = property_text(value).strip()
        if not text:
            continue
        if kind == "date" and "event_at" not in meta:
            meta["event_at"] = text          # The article's own date, never the fetch time.
        elif name.lower() == "published":
            meta["origin"]["published"] = text
        elif kind == "status":
            meta["origin"]["status_label"] = text
    tags = [property_text({"type": "multi_select", "multi_select": [option]}).strip()
            for name, value in ({} if mapped else properties).items() if value.get("type") == "multi_select"
            for option in value.get("multi_select") or []]
    if tags:
        meta["domain"] = sorted({t for t in tags if t})
    text = finalize(meta, f"# {title}\n\n{body}" if body.strip() else f"# {title}\n")
    parse_note(f"notion page {identifier}", text.encode())
    return meta["id"], meta["origin"]["content_sha256"], where, text


def import_snapshot(snapshot: Path, vault: Path, scope: str, subdir: str = "20-sources",
                    body_source: str = "auto", body_property: str | None = None,
                    title_property: str | None = None, dry_run: bool = False,
                    page_body_slugs: list[str] | None = None,
                    slug_property: str = "Slug", import_config: dict | None = None) -> dict:
    """Import every record, or nothing. Unchanged pages leave their note file untouched."""
    if not isinstance(scope, str) or not ID.fullmatch(scope):
        raise MemoryError("an explicit scope is required")
    if "/" in subdir or "\\" in subdir or subdir.startswith("."):
        raise MemoryError("subdir must be a single plain directory name")
    if body_source not in ("auto", "page", "property"):
        raise MemoryError("body-source must be auto, page or property")
    manifest, records = load_snapshot(snapshot)
    target = vault / subdir

    listed = set(page_body_slugs) if page_body_slugs is not None else None
    built, where, reasons, undecided = {}, {}, {}, []
    reports = {}
    for record in records:
        report = []
        try:
            identifier, _, source, text = build(record, manifest, scope, body_source,
                                                body_property, title_property,
                                                listed, slug_property, import_config, report)
        except BodyConflict as exc:
            undecided.append({"id": (record["page"].get("id") or "").replace("-", ""),
                              "reason": str(exc)})
            # Ambiguous records are held back and named, never guessed at.
            continue
        if identifier in built:
            raise MemoryError(f"duplicate page id {identifier} in the snapshot")
        if len(text.encode()) > MAX_NOTE_BYTES:
            raise MemoryError(f"{identifier}: note exceeds {MAX_NOTE_BYTES} bytes")
        reports[identifier] = report
        built[identifier] = text
        where[source] = where.get(source, 0) + 1
        reasons[identifier] = source

    groups = plan(target, built, scope)
    container = next((k for k in ("data_source_id", "database_id") if manifest.get(k)), None)
    existing = set()
    if container and target.is_dir():
        for path in target.glob("src-*.md"):
            note = parse_note(str(path), path.read_bytes())
            origin = note.metadata.get("origin", {})
            if note.metadata["scope"] == scope and origin.get("kind") == ORIGIN_KIND and \
                    origin.get(container, "").replace("-", "") == manifest[container].replace("-", ""):
                existing.add(note.metadata["id"])
    present = {"src-" + record["page"]["id"].replace("-", "").lower() for record in records}
    if not dry_run:
        write_all([(target / f"{i}.md", built[i])
                   for i in groups["created"] + groups["revised"]])
    return {"snapshot": str(snapshot.name), "records": len(records), "scope": scope,
            "subdir": subdir, "dry_run": dry_run,
            "retrieved_at": manifest.get("fetch_finished_at"),
            "body_from": where, **groups,
            "body_from_by_id": reasons, "conversion_reports": reports,
            "needs_body_choice": sorted(undecided, key=lambda item: item["id"]),
            "absence_checked": container is not None,
            "absent_from_snapshot": sorted(existing - present)}
