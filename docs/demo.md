# Five-minute demo

`examples/demo.py` walks through the main path of knowledge-memory on synthetic data:

```text
synthetic Markdown → validate → index → search / get → context → stale-index check → MCP
```

It needs no network access once installed, no credential, no GPU and no model download.
Each step runs the real `km` CLI or the `km-mcp` server and checks the result, so a non-zero
exit means the demo found a problem rather than just printing something different.

## Run it

Python 3.11+ with SQLite FTS5 (trigram tokenizer, SQLite 3.34 or later) is required.
Installing the `[mcp]` extra downloads the official MCP Python SDK; the demo itself makes no
network calls.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
.venv/bin/python examples/demo.py
```

Without the `[mcp]` extra, run `.venv/bin/python examples/demo.py --skip-mcp`. The MCP step is
then reported as `SKIPPED`, not as passed. Without either, the demo stops and says which
extra to install.

The demo copies `examples/context-vault` into a new temporary directory outside the checkout
(shown as `$DEMO`), writes the index and the generated context there, and removes the
directory at the end. Nothing is written to the repository or to the fixture.
Pass `--keep` to keep the workspace and try the commands by hand; delete it yourself afterwards.

The synthetic notes are written in Japanese, so the queries are too:
`線形` means "linear", `主張` means "claim" and the context goal `収量予測の見直しを再開する`
means "resume the yield-forecast review".

## What each step shows

1. **validate** — `km validate` checks each note's frontmatter: required fields, ID and scope
   format, status and claim kind, source references and dates. Active notes need a source.
2. **index** — `km index` builds a SQLite FTS5 index from the vault. The Markdown files are the
   source of truth; the index can be deleted and rebuilt at any time.
3. **search** — `km search` returns active records in the requested scope, each with its status,
   claim kind and source reference.
4. **lifecycle** — the same query is run three times. Candidate records (imported or
   AI-proposed, not yet approved by a person) appear only with `--include-candidates`;
   retracted records only with `--include-history`. A search with 0 hits does not show that
   the information does not exist.
5. **get** — `km get` returns the whole record with its sources, timestamps and SHA-256.
6. **context** — `km context` builds a task context for a project within a fixed character
   budget. It quotes records with their sources rather than summarising them, and reports
   which records were dropped to fit the budget.
7. **stale index** — the demo appends a line to a note in its copy of the vault. Search then
   refuses to answer from the outdated index until `km index` is run again.
8. **MCP** — the demo starts `km-mcp` over stdio and connects with the official MCP Python SDK
   client: `initialize`, `tools/list`, `search_memory`, `get_memory`, and a call that tries to
   pass its own `scope`, which the server refuses. Vault, database and scope are fixed when
   the server starts. The server process ends with the client session.

## What it does not show

- Search quality on real notes. The fixture is small and synthetic.
- Whether a particular AI agent or MCP client chooses to call the tools. See
  [Checking that an agent actually uses the server](mcp.md#checking-that-an-agent-actually-uses-the-server).
- Notion import, sync, inbox review and operations commands. See the [README](../README.md#documentation).

The demo is run by `tests/test_demo.py`, which is part of the normal test suite and of CI.
