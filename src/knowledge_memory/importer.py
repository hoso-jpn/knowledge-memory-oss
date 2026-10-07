"""Import a local Notion-style CSV export as candidate snapshots.

The CSV stays the original. Each note is a derived snapshot that records which file and
row it came from, when it was read, and what the source said, without claiming any of it
was checked against the live page.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .core import ID, MAX_NOTE_BYTES, MemoryError, parse_note
from .html_text import html_to_markdown
from .staging import digest as _digest, finalize, plan, write_all

ORIGIN_KIND = "notion_csv_export"
RENDER_VERSION = "csv/1"
MAX_CSV_BYTES = 50_000_000
NOTION_ID = re.compile(r"([0-9a-f]{32})")
UNSAFE = re.compile(r"[^a-z0-9._-]+")
# Column names are not guaranteed by the export, so they are detected and overridable.
CANDIDATES = {
    "id": ("url", "notion url", "id", "page id", "リンク", "url ", "public url"),
    "title": ("name", "title", "名前", "タイトル"),
    "body": ("body", "content", "html", "本文", "内容", "article", "text"),
    "date": ("date", "published date", "日付", "公開日"),
    "published": ("published", "公開"),
    "status": ("status", "ステータス", "状態"),
}


def read_csv(path: Path) -> tuple[str, list[dict]]:
    """Return the file digest and every row, keeping each cell exactly as exported."""
    if path.is_symlink():
        raise MemoryError("csv must not be a symlink")
    raw = path.read_bytes()
    if not raw:
        raise MemoryError(f"{path.name}: csv is empty")
    if len(raw) > MAX_CSV_BYTES:
        raise MemoryError(f"{path.name}: csv exceeds {MAX_CSV_BYTES} bytes")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MemoryError(f"{path.name}: expected UTF-8 csv") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames or not any(f and f.strip() for f in reader.fieldnames):
        raise MemoryError(f"{path.name}: csv header row is missing")
    rows = []
    for number, row in enumerate(reader, start=2):
        if None in row or any(v is None for v in row.values()):
            raise MemoryError(f"{path.name} row {number}: column count does not match the header")
        rows.append({"__row__": number, **{k.strip(): v for k, v in row.items()}})
    if not rows:
        raise MemoryError(f"{path.name}: csv has a header but no rows")
    return _digest(raw), rows


def resolve_columns(fields: list[str], overrides: dict) -> dict:
    """Pick the columns to read, preferring an explicit choice over detection."""
    lowered = {f.lower(): f for f in fields}
    chosen = {}
    for role, names in CANDIDATES.items():
        override = overrides.get(role)
        if override:
            if override not in fields:
                raise MemoryError(f"column {override!r} is not in the csv: {sorted(fields)}")
            chosen[role] = override
            continue
        chosen[role] = next((lowered[n] for n in names if n in lowered), None)
    for role in ("title", "body"):
        if not chosen[role]:
            raise MemoryError(f"no {role} column found; pass --{role}-column. "
                              f"csv columns: {sorted(fields)}")
    return chosen


def record_identifier(row: dict, columns: dict) -> tuple[str, str]:
    """Return the record id and how it was derived, so an unstable choice stays visible."""
    cell = (row.get(columns["id"]) or "").strip() if columns["id"] else ""
    found = NOTION_ID.search(cell.lower().replace("-", ""))
    if found:
        return found.group(1), "export id"
    if cell:
        slug = UNSAFE.sub("-", cell.lower()).strip("-")
        if slug:
            return slug[:100], "id column"
    title = (row.get(columns["title"]) or "").strip()
    if not title:
        raise MemoryError(f"row {row['__row__']}: no id column value and no title to fall back to")
    return _digest(title.encode())[:32], "title hash"


def build_note(row, columns, scope, file_name, file_sha256, retrieved_at):
    """Turn one row into note text. Raises MemoryError for anything unusable."""
    title = (row.get(columns["title"]) or "").strip()
    if not title:
        raise MemoryError(f"row {row['__row__']}: {columns['title']} is empty")
    record_id, strategy = record_identifier(row, columns)
    payload = {k: v for k, v in row.items() if k != "__row__"}
    meta = {
        "id": f"src-{record_id}",
        "title": title,
        "kind": "source",
        "status": "candidate",
        "scope": scope,
        "claim_kind": "source_report",
        "updated": retrieved_at[:10],
        "sources": [{"ref": f"notion-csv:{record_id}",
                     "locator": f"{file_name} row {row['__row__']}"}],
        "origin": {
            "kind": ORIGIN_KIND,
            "record_id": record_id,
            "content_sha256": _digest(json.dumps(payload, ensure_ascii=False,
                                                 sort_keys=True).encode()),
            "retrieved_at": retrieved_at,
            "source_file": file_name,
            "source_file_sha256": file_sha256,
            # How the note was produced, so changing it counts as a change.
            "render_settings": f"{RENDER_VERSION} "
                               f"title={columns['title']} body={columns['body']} "
                               f"id={columns['id'] or '-'}",
        },
    }
    date_cell = (row.get(columns["date"]) or "").strip() if columns["date"] else ""
    if date_cell:
        # The record's own time. Never the time we read the export.
        meta["event_at"] = date_cell
    for role in ("published", "status"):
        cell = (row.get(columns[role]) or "").strip() if columns[role] else ""
        if cell:
            meta["origin"]["published" if role == "published" else "status_label"] = cell
    body = html_to_markdown(row.get(columns["body"]) or "")
    text = finalize(meta, f"# {title}\n\n{body}" if body.strip() else f"# {title}\n")
    parse_note(f"row {row['__row__']}", text.encode())   # Reject before writing anything.
    return meta["id"], strategy, meta, text


def import_csv(csv_path: Path, vault: Path, scope: str, subdir: str = "20-sources",
               overrides: dict | None = None, raw_dir: Path | None = None,
               dry_run: bool = False) -> dict:
    """Import every row, or nothing. Unchanged rows leave their note file untouched."""
    if not isinstance(scope, str) or not ID.fullmatch(scope):
        raise MemoryError("an explicit scope is required")
    if os.sep in subdir or (os.altsep and os.altsep in subdir) or subdir.startswith("."):
        raise MemoryError("subdir must be a single plain directory name")
    target = vault / subdir
    if raw_dir is not None and raw_dir.resolve().is_relative_to(vault.resolve()):
        raise MemoryError("the raw store must sit outside the vault")

    file_sha256, rows = read_csv(csv_path)
    columns = resolve_columns([f for f in rows[0] if f != "__row__"], overrides or {})
    retrieved_at = datetime.now(timezone.utc).isoformat()

    built, rows_by_id, strategies = {}, {}, set()
    for row in rows:
        identifier, strategy, meta, text = build_note(row, columns, scope, csv_path.name,
                                                      file_sha256, retrieved_at)
        if identifier in built:
            raise MemoryError(f"row {row['__row__']}: duplicate record id {identifier}; "
                              f"choose a different --id-column")
        if len(text.encode()) > MAX_NOTE_BYTES:
            raise MemoryError(f"row {row['__row__']}: note exceeds {MAX_NOTE_BYTES} bytes")
        strategies.add(strategy)
        built[identifier] = text
        rows_by_id[identifier] = row

    groups = plan(target, built, scope)
    existing = {p.stem for p in target.glob("src-*.md")} if target.is_dir() else set()

    if not dry_run:
        pending = [(target / f"{i}.md", built[i]) for i in groups["created"] + groups["revised"]]
        if raw_dir is not None:
            pending += [(raw_dir / f"{i}.json",
                         json.dumps({k: v for k, v in rows_by_id[i].items() if k != "__row__"},
                                    ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                        for i in groups["created"] + groups["revised"]]
        write_all(pending)
    return {"csv": csv_path.name, "csv_sha256": file_sha256, "rows": len(rows),
            "scope": scope, "subdir": subdir, "dry_run": dry_run,
            "columns": {k: v for k, v in columns.items() if v},
            "record_id_from": sorted(strategies), **groups,
            "absent_from_csv": sorted(existing - built.keys())}
