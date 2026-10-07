"""A small strict Markdown store and a disposable SQLite search index."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

SCHEMA_VERSION = 2
MAX_NOTE_BYTES = 2_000_000
ID = re.compile(r"[a-z][a-z0-9._-]{0,127}\Z")
KINDS = {"note", "source", "idea", "log", "moc", "decision", "project", "procedure"}
STATUSES = {"candidate", "active", "superseded", "retracted"}
CLAIM_KINDS = {"user_statement", "observation", "source_report", "hypothesis", "interpretation"}
REQUIRED = {"id", "title", "kind", "status", "scope", "claim_kind", "updated", "sources"}
OPTIONAL = {"domain", "aliases", "related_ids", "supersedes", "verified_at", "event_at", "origin"}
# Provenance of an imported snapshot. Separate from event_at, which is the record's own time.
ORIGIN_REQUIRED = {"kind", "record_id", "content_sha256", "retrieved_at"}
ORIGIN_OPTIONAL = {"source_url", "source_file", "source_file_sha256", "source_edited_at",
                   "body_from", "body_rule", "render_settings", "render_sha256",
                   "published", "status_label", "database_id", "data_source_id",
                   "source_created_at", "source_metadata", "semantic_sha256"}
ORIGIN_DIGESTS = ("content_sha256", "source_file_sha256", "render_sha256", "semantic_sha256")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class MemoryError(ValueError):
    """Actionable input, schema, or index error."""


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate metadata instead of silently overwriting it."""


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise MemoryError("metadata keys must be unique strings")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


# FTS5 reads its MATCH argument as a NUL-terminated string, so a NUL in a query raises
# "unterminated string" (seen on SQLite 3.45.1 and 3.51.1), and on 3.45.1 the trigram
# tokenizer also dropped indexed text after a NUL. Map controls to spaces on both sides.
CONTROLS = {c: " " for c in [*range(0x20), *range(0x7F, 0xA0)]}


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).translate(CONTROLS).casefold().split())


def _date(value, name):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise MemoryError(f"{name}: datetime must have a timezone")
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        raise MemoryError(f"{name}: expected ISO date or timezone-aware datetime")
    try:
        if len(value) == 10:
            date.fromisoformat(value)
        else:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timezone missing")
    except ValueError as exc:
        raise MemoryError(f"{name}: invalid ISO date or timezone missing") from exc
    return value


@dataclass(frozen=True)
class Note:
    path: str
    sha256: str
    metadata: dict
    content: str
    body: str


def parse_note(path: str, raw: bytes) -> Note:
    if len(raw) > MAX_NOTE_BYTES:
        raise MemoryError(f"{path}: note exceeds {MAX_NOTE_BYTES} bytes")
    try:
        content = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise MemoryError(f"{path}: expected UTF-8") from exc
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise MemoryError(f"{path}: YAML frontmatter required")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise MemoryError(f"{path}: frontmatter terminator missing")
    try:
        meta = yaml.load("".join(lines[1:end]), Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise MemoryError(f"{path}: invalid YAML") from exc
    if not isinstance(meta, dict):
        raise MemoryError(f"{path}: metadata must be a mapping")
    if REQUIRED - meta.keys() or meta.keys() - REQUIRED - OPTIONAL:
        raise MemoryError(f"{path}: missing={sorted(REQUIRED - meta.keys())}; "
                          f"unknown={sorted(meta.keys() - REQUIRED - OPTIONAL)}")
    for field in ("id", "title", "kind", "status", "scope", "claim_kind"):
        if not isinstance(meta[field], str) or not meta[field].strip():
            raise MemoryError(f"{path}: {field} must be a non-empty string")
    if not ID.fullmatch(meta["id"]) or not ID.fullmatch(meta["scope"]):
        raise MemoryError(f"{path}: id and scope require stable lowercase identifiers")
    for field, choices in (("kind", KINDS), ("status", STATUSES), ("claim_kind", CLAIM_KINDS)):
        if meta[field] not in choices:
            raise MemoryError(f"{path}: invalid {field}")
    for field in ("updated", "verified_at", "event_at"):
        if field in meta and meta[field] is not None:
            meta[field] = _date(meta[field], field)
    if meta["updated"] is None:
        raise MemoryError(f"{path}: updated is required")
    sources = meta["sources"]
    if not isinstance(sources, list):
        raise MemoryError(f"{path}: sources must be a list")
    for source in sources:
        if not isinstance(source, dict) or set(source) != {"ref", "locator"}:
            raise MemoryError(f"{path}: each source requires ref and locator")
        if not all(isinstance(v, str) and v.strip() for v in source.values()):
            raise MemoryError(f"{path}: source fields must be non-empty strings")
    if meta["status"] == "active" and not sources:
        raise MemoryError(f"{path}: active notes require a source reference")
    for field in ("domain", "aliases", "related_ids", "supersedes"):
        values = meta.setdefault(field, [])
        if not isinstance(values, list) or not all(isinstance(v, str) and v.strip() for v in values):
            raise MemoryError(f"{path}: {field} must be a list of non-empty strings")
        if len(values) != len(set(values)):
            raise MemoryError(f"{path}: duplicate {field}")
    if "origin" in meta:
        _origin(path, meta)
    return Note(path, hashlib.sha256(raw).hexdigest(), meta, content, "".join(lines[end + 1:]))


def _origin(path, meta):
    origin = meta["origin"]
    if not isinstance(origin, dict) or ORIGIN_REQUIRED - origin.keys() \
            or origin.keys() - ORIGIN_REQUIRED - ORIGIN_OPTIONAL:
        raise MemoryError(f"{path}: origin requires exactly {sorted(ORIGIN_REQUIRED)} "
                          f"plus optional {sorted(ORIGIN_OPTIONAL)}")
    for field in sorted(origin.keys() - {"retrieved_at", "source_edited_at", "source_created_at", "source_metadata"}):
        if not isinstance(origin[field], str) or not origin[field].strip():
            raise MemoryError(f"{path}: origin.{field} must be a non-empty string")
    for field in ORIGIN_DIGESTS:
        if field in origin and not SHA256.fullmatch(origin[field]):
            raise MemoryError(f"{path}: origin.{field} must be a lowercase sha256 digest")
    if "source_file" in origin and ("/" in origin["source_file"] or "\\" in origin["source_file"]):
        raise MemoryError(f"{path}: origin.source_file must be a bare file name, not a path")
    for field in ("retrieved_at", "source_edited_at", "source_created_at"):
        if field in origin:
            origin[field] = _date(origin[field], f"origin.{field}")
    if "source_metadata" in origin:
        from .notion_config import validate_source_metadata
        validate_source_metadata(origin["source_metadata"])
    # An imported snapshot reports what a source said; it does not establish it.
    if meta["claim_kind"] != "source_report":
        raise MemoryError(f"{path}: notes with origin must use claim_kind source_report")


def snapshot(vault: Path) -> dict[str, bytes]:
    root = vault.resolve()
    if not root.is_dir():
        raise MemoryError("vault directory is unavailable")
    found = {}
    for folder, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in dirs + files:
            candidate = Path(folder) / name
            if candidate.is_symlink():
                raise MemoryError(f"symlinks are not allowed in vault: {candidate.relative_to(root)}")
        for name in sorted(files):
            if name.startswith(".") or not name.endswith(".md"):
                continue
            path = Path(folder) / name
            if path.stat().st_size > MAX_NOTE_BYTES:
                raise MemoryError(f"{path.relative_to(root)}: note too large")
            found[path.relative_to(root).as_posix()] = path.read_bytes()
    return found


def load_vault(vault: Path) -> list[Note]:
    notes = [parse_note(path, raw) for path, raw in sorted(snapshot(vault).items())]
    by_id = {}
    for note in notes:
        identifier = note.metadata["id"]
        if identifier in by_id:
            raise MemoryError(f"duplicate id: {identifier}")
        by_id[identifier] = note
    for note in notes:
        meta = note.metadata
        for identifier in meta["related_ids"] + meta["supersedes"]:
            if identifier not in by_id or identifier == meta["id"]:
                raise MemoryError(f"{note.path}: missing or self-referencing link: {identifier}")
            if by_id[identifier].metadata["scope"] != meta["scope"]:
                raise MemoryError(f"{note.path}: links must remain inside one scope")
        for identifier in meta["supersedes"]:
            if by_id[identifier].metadata["status"] != "superseded":
                raise MemoryError(f"{note.path}: replaced note must be marked superseded")
    return notes


def build_index(vault: Path, database: Path) -> int:
    if database.is_symlink():
        raise MemoryError("database must not be a symlink")
    database = database.resolve()
    if database.suffix != ".sqlite3" or database.is_relative_to(vault.resolve()):
        raise MemoryError("database must be a .sqlite3 file outside the vault")
    notes = load_vault(vault)  # Validation failure must preserve the existing index.
    database.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix="km-index-", suffix=".sqlite3", dir=database.parent)
    os.close(handle)
    temporary = Path(temp_name)
    con = None
    try:
        with sqlite3.connect(temporary) as con:
            con.executescript("""
                CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE notes (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL, sha256 TEXT NOT NULL,
                    scope TEXT NOT NULL, status TEXT NOT NULL, title TEXT NOT NULL,
                    metadata TEXT NOT NULL, content TEXT NOT NULL, body TEXT NOT NULL,
                    search_text TEXT NOT NULL
                );
                CREATE VIRTUAL TABLE search USING fts5(text, tokenize='trigram');
            """)
            for key, value in {
                "schema_version": str(SCHEMA_VERSION),
                "vault": str(vault.resolve()),
                "indexed_at": datetime.now(timezone.utc).isoformat(),
            }.items():
                con.execute("INSERT INTO meta VALUES (?, ?)", (key, value))
            for note in notes:
                m = note.metadata
                text = normalize("\n".join([m["title"], note.body, *m["aliases"], *m["domain"]]))
                cursor = con.execute("INSERT INTO notes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                     (m["id"], note.path, note.sha256, m["scope"], m["status"],
                                      m["title"], json.dumps(m, ensure_ascii=False), note.content,
                                      note.body, text))
                con.execute("INSERT INTO search(rowid, text) VALUES (?, ?)", (cursor.lastrowid, text))
        con.close()
        os.replace(temporary, database)
    except sqlite3.OperationalError as exc:
        raise MemoryError("SQLite FTS5 with trigram support is required; index was not replaced") from exc
    finally:
        if con is not None:
            con.close()
        temporary.unlink(missing_ok=True)
    return len(notes)


def _connect(database: Path) -> sqlite3.Connection:
    try:
        con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        meta = dict(con.execute("SELECT key, value FROM meta"))
        if meta.get("schema_version") != str(SCHEMA_VERSION):
            raise MemoryError("index schema changed; rebuild the index")
        expected = dict(con.execute("SELECT path, sha256 FROM notes"))
        current = {p: hashlib.sha256(raw).hexdigest() for p, raw in snapshot(Path(meta["vault"])).items()}
        if expected != current:
            raise MemoryError("vault changed after indexing; rebuild the index")
        guard_path = Path(meta["vault"]) / ".km-source-status.json"
        blocked = set()
        if guard_path.exists():
            try:
                if guard_path.is_symlink():
                    raise ValueError()
                guard = json.loads(guard_path.read_text())
                if not isinstance(guard, dict):
                    raise ValueError()
                for entry in guard.values():
                    if not isinstance(entry, dict) or not ID.fullmatch(entry.get("note_id", "")) or "status" not in entry:
                        raise ValueError()
                    if entry["status"] != "current":
                        blocked.add(entry["note_id"])
            except (ValueError, OSError, TypeError):
                raise MemoryError("invalid source availability ledger; repair sync state") from None
        con.create_function("source_allowed", 1, lambda identifier: identifier not in blocked)
        return con
    except Exception as exc:
        if "con" in locals():
            con.close()
        if isinstance(exc, (sqlite3.Error, KeyError)):
            raise MemoryError("invalid or unavailable index; rebuild it") from exc
        raise


def _states(include_candidates: bool, include_history: bool) -> list[str]:
    return ["active"] + (["candidate"] if include_candidates else []) + (
        ["superseded", "retracted"] if include_history else [])


def _scope(scope):
    if not isinstance(scope, str) or not ID.fullmatch(scope):
        raise MemoryError("an explicit scope is required")


def search_notes(database: Path, query: str, scope: str, limit: int = 10,
                 include_candidates: bool = False, include_history: bool = False) -> list[dict]:
    _scope(scope)
    if not isinstance(query, str) or not query.strip() or len(query) > 512:
        raise MemoryError("query must contain 1 to 512 characters")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise MemoryError("limit must be between 1 and 50")
    term = normalize(query)
    if not term:
        raise MemoryError("query is empty after normalization")
    states = _states(include_candidates, include_history)
    placeholders = ",".join("?" for _ in states)
    where = f"n.scope = ? AND n.status IN ({placeholders}) AND source_allowed(n.id)"
    params = [scope, *states]
    if len(term) >= 3:
        quoted = '"' + term.replace('"', '""') + '"'
        sql = f"""SELECT n.*, snippet(search, 0, '[', ']', '…', 24) AS excerpt
                  FROM search JOIN notes n ON n.rowid = search.rowid
                  WHERE search MATCH ? AND {where}
                  ORDER BY bm25(search), n.id LIMIT ?"""
        params = [quoted, *params, limit]
    else:
        # Literal short-term search: no LIKE wildcards or FTS syntax interpretation.
        sql = f"""SELECT n.*, substr(n.search_text, max(1, instr(n.search_text, ?) - 50), 160) AS excerpt
                  FROM notes n WHERE instr(n.search_text, ?) > 0 AND {where}
                  ORDER BY n.id LIMIT ?"""
        params = [term, term, *params, limit]
    con = _connect(database)
    try:
        rows = con.execute(sql, params).fetchall()
        return [{"id": r["id"], "title": r["title"], "path": r["path"],
                 "sha256": r["sha256"], "metadata": json.loads(r["metadata"]),
                 "excerpt": r["excerpt"], "excerpt_normalized": True} for r in rows]
    finally:
        con.close()


def read_notes(database: Path, scope: str, include_candidates: bool = False,
               include_history: bool = False, ids: list[str] | None = None) -> list[dict]:
    """Read many notes over one connection, so a caller need not rehash the vault per note."""
    _scope(scope)
    states = _states(include_candidates, include_history)
    where = [f"scope = ?", f"status IN ({','.join('?' for _ in states)})", "source_allowed(id)"]
    params = [scope, *states]
    if ids is not None:
        if not ids:
            return []
        where.append(f"id IN ({','.join('?' for _ in ids)})")
        params.extend(ids)
    con = _connect(database)
    try:
        rows = con.execute(f"SELECT * FROM notes WHERE {' AND '.join(where)} ORDER BY id",
                           params).fetchall()
        return [{"id": r["id"], "path": r["path"], "sha256": r["sha256"],
                 "metadata": json.loads(r["metadata"]), "content": r["content"],
                 "body": r["body"]} for r in rows]
    finally:
        con.close()


def index_meta(database: Path) -> dict:
    """The index header, including the vault it was built from."""
    con = _connect(database)
    try:
        return dict(con.execute("SELECT key, value FROM meta"))
    finally:
        con.close()


def get_note(database: Path, identifier: str, scope: str,
             include_candidates: bool = False, include_history: bool = False) -> dict:
    _scope(scope)
    states = _states(include_candidates, include_history)
    placeholders = ",".join("?" for _ in states)
    con = _connect(database)
    try:
        row = con.execute(f"SELECT * FROM notes WHERE id = ? AND scope = ? AND status IN ({placeholders}) AND source_allowed(id)",
                          [identifier, scope, *states]).fetchone()
        if row is None:
            raise MemoryError("note is unavailable in the requested scope and states")
        return {"id": row["id"], "path": row["path"], "sha256": row["sha256"],
                "metadata": json.loads(row["metadata"]), "content": row["content"]}
    finally:
        con.close()
