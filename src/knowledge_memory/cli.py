"""Human/agent-readable JSON CLI. Network adapters are separate future work."""

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .context import build_context, write_context
from .core import MemoryError, build_index, get_note, load_vault, search_notes
from .evaluation import run_evaluation
from .importer import import_csv
from .notion_api import API_BASE, NOTION_VERSION, fetch_snapshot
from .notion_config import load_config
from .notion_import import describe, import_snapshot, load_snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(prog="km")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "index"):
        command = sub.add_parser(name)
        command.add_argument("--vault", type=Path, required=True)
        if name == "index":
            command.add_argument("--db", type=Path, required=True)
    command = sub.add_parser("import-csv")
    command.add_argument("--csv", type=Path, required=True)
    command.add_argument("--vault", type=Path, required=True)
    command.add_argument("--scope", required=True)
    command.add_argument("--subdir", default="20-sources")
    command.add_argument("--raw", type=Path)
    command.add_argument("--dry-run", action="store_true")
    for role in ("id", "title", "body", "date", "published", "status"):
        command.add_argument(f"--{role}-column")
    command = sub.add_parser("fetch-notion")
    command.add_argument("--out", type=Path, required=True)
    command.add_argument("--data-source")
    command.add_argument("--database", help="resolve a single data source; multiple sources require --data-source")
    command.add_argument("--token-env", default="NOTION_TOKEN")
    command.add_argument("--vault", type=Path)
    command.add_argument("--api-base", default=API_BASE)
    command.add_argument("--notion-version", default=NOTION_VERSION)
    command = sub.add_parser("describe-snapshot")
    command.add_argument("--snapshot", type=Path, required=True)
    command = sub.add_parser("import-notion")
    command.add_argument("--snapshot", type=Path, required=True)
    command.add_argument("--vault", type=Path, required=True)
    command.add_argument("--scope", required=True)
    command.add_argument("--subdir", default="20-sources")
    command.add_argument("--import-config", type=Path)
    command.add_argument("--body-source", choices=("auto", "page", "property"), default="auto")
    command.add_argument("--body-property")
    command.add_argument("--title-property")
    command.add_argument("--slug-property", default="Slug")
    command.add_argument("--page-body-slugs",
                         help="comma-separated slugs whose body comes from the Notion page "
                              "body; every other record uses the body property")
    command.add_argument("--dry-run", action="store_true")
    command = sub.add_parser("context")
    command.add_argument("--db", type=Path, required=True)
    command.add_argument("--scope", required=True)
    command.add_argument("--goal", required=True)
    command.add_argument("--project")
    command.add_argument("--budget", type=int, default=8000)
    command.add_argument("--budget-unit", choices=("chars", "tokens"), default="chars")
    command.add_argument("--depth", type=int, default=2)
    command.add_argument("--include-candidates", action="store_true")
    command.add_argument("--include-history", action="store_true")
    command.add_argument("--out", type=Path)
    command.add_argument("--print", action="store_true",
                         help="write the context to stdout instead of the JSON summary")
    command = sub.add_parser("evaluate")
    command.add_argument("--db", type=Path, required=True)
    command.add_argument("--questions", type=Path, required=True)
    command.add_argument("--scope", required=True)
    command.add_argument("--include-candidates", action="store_true")
    command.add_argument("--limit", type=int, default=10)
    for name in ("search", "get"):
        command = sub.add_parser(name)
        command.add_argument("--db", type=Path, required=True)
        command.add_argument("--scope", required=True)
        command.add_argument("--include-candidates", action="store_true")
        command.add_argument("--include-history", action="store_true")
        command.add_argument("query" if name == "search" else "id")
        if name == "search":
            command.add_argument("--limit", type=int, default=10)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            result = {"valid": True, "notes": len(load_vault(args.vault))}
        elif args.command == "index":
            result = {"indexed": build_index(args.vault, args.db)}
        elif args.command == "import-csv":
            overrides = {role: getattr(args, f"{role}_column")
                         for role in ("id", "title", "body", "date", "published", "status")}
            result = import_csv(args.csv, args.vault, args.scope, args.subdir,
                                overrides, args.raw, args.dry_run)
        elif args.command == "fetch-notion":
            token = os.environ.get(args.token_env, "")
            if not token:
                raise MemoryError(f"environment variable {args.token_env} is not set; "
                                  f"export the Notion integration token there")
            result = fetch_snapshot(token, args.out, args.data_source, args.database,
                                    args.api_base, args.notion_version, args.vault)
        elif args.command == "describe-snapshot":
            result = describe(*load_snapshot(args.snapshot))
        elif args.command == "import-notion":
            listed = ([s.strip() for s in args.page_body_slugs.split(",") if s.strip()]
                      if args.page_body_slugs is not None else None)
            result = import_snapshot(args.snapshot, args.vault, args.scope, args.subdir,
                                     args.body_source, args.body_property,
                                     args.title_property, args.dry_run, listed,
                                     args.slug_property,
                                     load_config(args.import_config) if args.import_config else None)
        elif args.command == "context":
            built = build_context(args.db, args.scope, args.goal, args.project, args.budget,
                                  args.budget_unit, args.depth, args.include_candidates,
                                  args.include_history)
            if args.out is not None:
                write_context(built, args.out, args.db)
            if getattr(args, "print"):
                print(built["text"], end="")
                return 0
            result = {k: v for k, v in built.items() if k != "text"}
            result["out"] = str(args.out) if args.out else None
        elif args.command == "evaluate":
            result = run_evaluation(args.db, args.questions, args.scope,
                                    args.include_candidates, args.limit)
        else:
            options = {"include_candidates": args.include_candidates,
                       "include_history": args.include_history}
            if args.command == "search":
                result = search_notes(args.db, args.query, args.scope, args.limit, **options)
            else:
                result = get_note(args.db, args.id, args.scope, **options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (MemoryError, OSError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
