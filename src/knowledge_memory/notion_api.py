"""Read-only Notion fetch into a local snapshot.

Notion stays the original. This writes what the API returned, verbatim, into a snapshot
directory outside the vault. Nothing here edits, creates or publishes anything in Notion:
only GET and the documented query POST are used.

A partial fetch is never written. Everything is collected and checked first, and only a
complete result replaces the previous snapshot.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .core import MemoryError

API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"
PAGE_SIZE = 100
MAX_PAGES = 10_000
RETRIES = 3
MAX_RETRY_WAIT = 30
SUPPORTED_VERSIONS = {"2022-06-28", "2025-09-03"}


class NotionError(MemoryError):
    """A fetch could not be completed. The previous snapshot is left alone."""

    def __init__(self, message, *, retry_after_seconds=None):
        self.retry_after_seconds = retry_after_seconds
        self.retry_at = None
        if retry_after_seconds is not None:
            try:
                self.retry_at = (datetime.now(timezone.utc) +
                                 timedelta(seconds=retry_after_seconds)).isoformat()
            except OverflowError:
                pass
            message += f"; retry_after_seconds={retry_after_seconds}; retry_at={self.retry_at}"
        super().__init__(message)


def _request(base, token, version, method, path, body=None, timeout=60):
    url = f"{base.rstrip('/')}{path}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Notion-Version", version)
    request.add_header("Accept", "application/json")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    waited = 0.0
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Response bodies, paths and credentials are never part of a diagnostic.
            code, retry_after = exc.code, exc.headers.get("Retry-After")
            exc.close()
            if code in (401, 403) and retry_after is None:
                raise NotionError(f"Notion refused the request ({code}); check access") from None
            if code == 404:
                raise NotionError("Notion returned 404; missing or inaccessible") from None
            if code not in (409, 429) and code < 500 and retry_after is None:
                raise NotionError(f"Notion returned HTTP {code}") from None
            try:
                delay = float(retry_after) if retry_after is not None else 2 ** attempt
            except (ValueError, TypeError):
                raise NotionError("retry_later: invalid Retry-After") from None
            if not math.isfinite(delay) or delay < 0:
                raise NotionError("retry_later: invalid Retry-After") from None
            if attempt == RETRIES - 1:
                raise NotionError("retry_later: retry budget exhausted",
                                  retry_after_seconds=delay) from None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError):
            if attempt == RETRIES - 1:
                raise NotionError("fetch_failed: transport or invalid JSON") from None
            delay = 2 ** attempt
        if delay > MAX_RETRY_WAIT - waited:
            raise NotionError("retry_later: retry budget exhausted",
                              retry_after_seconds=delay) from None
        time.sleep(delay)
        waited += delay
    raise NotionError("fetch_failed")


def _page_of(result, path):
    """Check one paged response. An inconsistent page means the fetch is not complete."""
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        raise NotionError(f"unexpected response for {path}: results is not a list")
    status = result.get("request_status")
    if status is not None and (not isinstance(status, dict) or status.get("type") != "complete"):
        raise NotionError("incomplete response: request_status is not complete")
    cursor = result.get("next_cursor")
    if result.get("has_more"):
        if not isinstance(cursor, str) or not cursor:
            raise NotionError(f"{path} reports more results but returns no cursor; "
                              f"the fetch is incomplete")
        return result["results"], cursor
    if cursor:
        raise NotionError(f"{path} reports no more results but returns a cursor; "
                          f"the response is inconsistent")
    return result["results"], None


def _follow(fetch, path):
    """Call fetch(cursor) until it is done, refusing a cursor that repeats or stalls."""
    items, cursor, seen = [], None, set()
    for _ in range(MAX_PAGES):
        batch, cursor = _page_of(fetch(cursor), path)
        items.extend(batch)
        if cursor is None:
            return items
        if cursor in seen:
            raise NotionError(f"pagination for {path} did not advance; fetch is incomplete")
        seen.add(cursor)
    raise NotionError(f"pagination for {path} exceeded {MAX_PAGES} requests")


def _paginate(call, path, body=None):
    def fetch(cursor):
        if body is None:
            query = f"{path}?page_size={PAGE_SIZE}" + (f"&start_cursor={cursor}" if cursor else "")
            return call("GET", query)
        payload = dict(body, page_size=PAGE_SIZE)
        if cursor:
            payload["start_cursor"] = cursor
        return call("POST", path, payload)

    return _follow(fetch, path)


def _blocks(call, block_id, depth=0):
    """Blocks plus their children. Notion nests, so an unvisited child loses content."""
    if depth > 20:
        raise NotionError(f"block nesting under {block_id} is deeper than 20 levels")
    children = _paginate(call, f"/blocks/{block_id}/children")
    for block in children:
        if block.get("has_children"):
            block["_children"] = _blocks(call, block["id"], depth + 1)
    return children


def _full_property(call, page_id, property_id):
    """A rich_text property is truncated in query results, so read it through its own endpoint."""
    path = f"/pages/{page_id}/properties/{property_id}"
    first = call("GET", f"{path}?page_size={PAGE_SIZE}")
    if not isinstance(first, dict):
        raise NotionError(f"unexpected response for {path}")
    if first.get("object") != "list":
        return first

    def fetch(cursor):
        if cursor is None:
            return first
        return call("GET", f"{path}?page_size={PAGE_SIZE}&start_cursor={cursor}")

    return {"object": "list", "results": _follow(fetch, path), "has_more": False}


def fetch_snapshot(token: str, out: Path, data_source: str | None = None,
                   database: str | None = None, base: str = API_BASE,
                   version: str = NOTION_VERSION, vault: Path | None = None) -> dict:
    """Fetch the schema, every page and every page's body into `out`. All or nothing."""
    if not token or not token.strip():
        raise NotionError("no Notion token; set one in the environment and pass --token-env")
    if bool(data_source) == bool(database):
        raise NotionError("pass exactly one of --data-source or --database")
    if vault is not None and out.resolve().is_relative_to(vault.resolve()):
        raise NotionError("the snapshot directory must sit outside the vault")

    if version not in SUPPORTED_VERSIONS:
        raise NotionError("unsupported Notion version; use 2025-09-03 or legacy 2022-06-28")
    if data_source and version == "2022-06-28":
        raise NotionError("--data-source requires Notion version 2025-09-03")

    def call(method, path, body=None):
        return _request(base, token, version, method, path, body)

    started = datetime.now(timezone.utc).isoformat()
    if database and version == "2025-09-03":
        database_schema = call("GET", f"/databases/{database}")
        sources = database_schema.get("data_sources") if isinstance(database_schema, dict) else None
        if (not isinstance(sources, list) or len(sources) != 1 or
                not isinstance(sources[0], dict) or not isinstance(sources[0].get("id"), str)
                or not sources[0]["id"]):
            raise NotionError("database must expose exactly one data source; select --data-source explicitly")
        data_source = sources[0]["id"]
    if data_source:
        schema = call("GET", f"/data_sources/{data_source}")
        pages = _paginate(call, f"/data_sources/{data_source}/query", {})
        if len(pages) >= 10_000:
            raise NotionError("incomplete response: enumeration_limit")
        container = {"data_source_id": data_source}
        parent_database = (schema.get("parent") or {}).get("database_id")
        if database and parent_database and database.replace('-', '').lower() != parent_database.replace('-', '').lower():
            raise NotionError("database/data source parent mismatch")
        if database or parent_database:
            container["database_id"] = database or parent_database
    else:
        # Kept only for callers who explicitly pin the pre-data-source API.
        schema = call("GET", f"/databases/{database}")
        pages = _paginate(call, f"/databases/{database}/query", {})
        container = {"database_id": database}

    properties = schema.get("properties") or {}
    records, skipped = [], []
    for page in pages:
        if page.get("object") != "page":
            skipped.append({"id": page.get("id"), "reason": f"not a page: {page.get('object')}"})
            continue
        if page.get("archived") or page.get("in_trash"):
            skipped.append({"id": page.get("id"), "reason": "archived or in trash"})
            continue
        full = {}
        for name, definition in properties.items():
            if definition.get("type") in ("rich_text", "title"):
                value = page.get("properties", {}).get(name)
                if value is not None:
                    full[name] = _full_property(call, page["id"], value["id"])
        records.append({"page": page, "properties_full": full,
                        "blocks": _blocks(call, page["id"])})

    finished = datetime.now(timezone.utc).isoformat()
    manifest = {**container, "notion_version": version, "api_base": base,
                "fetch_started_at": started, "fetch_finished_at": finished,
                "schema_properties": {n: d.get("type") for n, d in sorted(properties.items())},
                "pages_returned": len(pages), "records_saved": len(records),
                "skipped": skipped, "complete": True}

    staging = Path(tempfile.mkdtemp(prefix=".km-notion-", dir=out.parent if out.parent.exists()
                                    else Path(tempfile.gettempdir())))
    try:
        for record in records:
            (staging / f"{record['page']['id']}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8")
        previous = out.with_name(out.name + ".previous")
        out.parent.mkdir(parents=True, exist_ok=True)
        moved = False
        if out.exists():
            shutil.rmtree(previous, ignore_errors=True)
            os.replace(out, previous)
            moved = True
        try:
            os.replace(staging, out)
        except BaseException:
            if moved:
                os.replace(previous, out)   # put the earlier snapshot back where it was
            raise
        shutil.rmtree(previous, ignore_errors=True)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return manifest
