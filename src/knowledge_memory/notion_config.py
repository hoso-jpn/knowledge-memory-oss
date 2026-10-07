"""Explicit property mapping shared by snapshot import and selected-source sync."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from .core import MemoryError, UniqueLoader, _date
from .notion_blocks import property_text

PROPERTY_TYPES = {
    "title": "title", "domain": "multi_select", "source_type": "select",
    "editorial_status": "select", "source_description": "rich_text",
    "reference_url": "url", "document_date": "date", "original_filename": "rich_text",
}
SOURCE_FIELDS = set(PROPERTY_TYPES) - {"title", "domain"}


def validate_config(config: dict) -> dict:
    if not isinstance(config, dict) or set(config) != {"version", "body_source", "properties"}:
        raise MemoryError("Notion import config requires version, body_source, properties")
    if type(config["version"]) is not int or config["version"] != 1 or config["body_source"] != "page":
        raise MemoryError("Notion import config supports version 1 and body_source page")
    properties = config["properties"]
    if not isinstance(properties, dict) or set(properties) != set(PROPERTY_TYPES):
        raise MemoryError(f"Notion property mapping requires exactly {sorted(PROPERTY_TYPES)}")
    if not all(isinstance(v, str) and v.strip() for v in properties.values()) \
            or len(set(properties.values())) != len(properties):
        raise MemoryError("mapped Notion property names must be nonempty and distinct")
    return config


def load_config(path: Path) -> dict:
    try:
        return validate_config(yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader))
    except (OSError, yaml.YAMLError) as exc:
        raise MemoryError("cannot read Notion import config") from exc


def settings(config: dict) -> str:
    return "mapping/1 " + json.dumps(validate_config(config), ensure_ascii=False, sort_keys=True)


def mapped_metadata(record: dict, config: dict) -> tuple[str, list[str], dict]:
    """Read named, typed properties; unrelated dates and tags cannot substitute for them."""
    validate_config(config)
    properties = record["page"].get("properties") or {}
    full = record.get("properties_full") or {}
    values = {}
    for field, kind in PROPERTY_TYPES.items():
        name = config["properties"][field]
        value = properties.get(name)
        if not isinstance(value, dict) or value.get("type") != kind:
            raise MemoryError(f"mapped property {name!r} must exist with type {kind}")
        values[field] = value
    title = property_text(full.get(config["properties"]["title"], values["title"])).strip()
    domain = sorted({entry["name"] for entry in values["domain"].get("multi_select", [])})
    source = {}
    for field in sorted(SOURCE_FIELDS):
        value = values[field]
        if field == "document_date":
            source[field] = value.get("date")
        else:
            source[field] = property_text(full.get(config["properties"][field], value)) or None
    validate_source_metadata(source)
    return title, domain, source


def validate_source_metadata(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != SOURCE_FIELDS:
        raise MemoryError(f"origin.source_metadata requires exactly {sorted(SOURCE_FIELDS)}")
    for field in SOURCE_FIELDS - {"document_date"}:
        if value[field] is not None and not isinstance(value[field], str):
            raise MemoryError(f"source_metadata.{field} must be a string or null")
    document_date = value["document_date"]
    if document_date is not None:
        if not isinstance(document_date, dict) or "start" not in document_date \
                or document_date.keys() - {"start", "end", "time_zone"}:
            raise MemoryError("document_date requires start, optional end/time_zone")
        for field in ("start", "end"):
            if document_date.get(field) is not None:
                document_date[field] = _date(document_date[field], f"document_date.{field}")
        if document_date["start"] is None:
            raise MemoryError("document_date.start cannot be null")
        if document_date.get("time_zone") is not None \
                and not isinstance(document_date["time_zone"], str):
            raise MemoryError("document_date.time_zone must be a string or null")
