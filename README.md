# knowledge-memory

**English** | [日本語](README.ja.md)

> **Local-first, source-grounded memory infrastructure for AI agents.**

knowledge-memory gives AI agents a long-term memory without losing the **source, time,
lifecycle state and correction history** of what it stores. Human approval is the boundary
between proposed and accepted knowledge, and agents receive scope- and state-filtered search
results and contexts over MCP, with explicit limits.

Markdown files are the source of truth. The SQLite search index can be rebuilt from them.
The default search and MCP paths need no GPU and no external LLM API.

## Why

In many "AI memory" and RAG setups, retrieved information tends to lose its context:

- it is unclear when the information dates from
- what it was based on is unclear
- old and new decisions get mixed together
- what an AI proposed gets mixed with what a person confirmed
- once something is indexed, it is unclear when deletion or expiry takes effect
- private and work sources are separated only by a logical filter

knowledge-memory puts **provenance, lifecycle, bounded retrieval and human approval** at the
center of the design, not only search accuracy.

| Aspect | Typical RAG / vector store | Second-brain note apps | knowledge-memory |
| --- | --- | --- | --- |
| Source of truth | Embedded chunks | Notes inside the app | Markdown files; the index can be rebuilt |
| Source and time | Optional metadata | Optional | Active records require a source; edit, retrieval, event and verification times are separate fields |
| AI writes | Often added directly | Written by people | AI proposals start as candidates; making a record active is an explicit human step |
| Outdated or wrong records | Overwritten or deleted | Tidied up by hand | Kept as superseded / retracted and excluded from normal search |
| Handing to an AI | Top-k results | Copy and paste | Filtered by scope and state, returned through MCP with result and context limits (whole-response limits in bounded mode) |
| Semantic search | Central | Depends on the app | Default is literal search (FTS5 trigram); semantic search is an optional experiment |

Beating general-purpose RAG on retrieval quality is not a goal. The difference is that an AI
and a person can check where a retrieved record came from, when, and in what state.

## Core principles

- **Source-grounded** — active records carry at least one source reference; imported records carry their retrieval time
- **Local-first** — the vault and the SQLite index live on the user's machine. Search, record retrieval and MCP do not connect to external services
- **Rebuildable index** — the SQLite index is not the original; it can be rebuilt from the vault
- **Human-in-the-loop** — AI-generated records start as candidates
- **Explicit lifecycle** — candidate / active / superseded / retracted
- **Bounded context** — search results and task contexts have explicit limits; bounded MCP mode also limits each whole response
- **Fail closed** — a stale index or an incomplete fetch is not treated as success
- **Boundary aware** — keep personal and work data in separate vaults, databases and processes, not only separate scopes

## Architecture

```text
Notion / CSV / inbox text
          │
          ▼
   raw snapshot (outside the vault)
          │
          ▼
┌──────── Markdown vault ────────┐
│  candidate ──human review──► active
│  active ──► superseded / retracted
└────────────────────────────────┘
          │  km index (rebuildable)
          ▼
    SQLite FTS5 index
       │        │
       ▼        ▼
      CLI   stdio MCP ──► AI agent
```

Candidates and active records live in the same vault and are told apart by `status`.
When Notion is the original, the snapshot and the Markdown derived from it are kept apart.
The SQLite index can be rebuilt from the vault at any time.

## What it does

- YAML frontmatter validation (`km validate`)
- SQLite FTS5 trigram search, plus literal search for terms shorter than 3 characters (`km search`)
- state- and scope-aware search, stale-index detection
- source-grounded record retrieval (`km get`)
- task context generation (`km context`)
- read-only stdio MCP server, with a bounded mode that enforces response limits (`km-mcp`)
- read-only Notion fetch and import, CSV import (`km fetch-notion` / `km import-notion` / `km import-csv`)
- explicit synchronization and availability state (`km-sync`)
- candidate review workflow (`km-inbox`; non-LLM extraction by default, optionally a local model on loopback)
- backup / restore / recovery check / measurement (`km-ops`)
- question-set evaluation (`km evaluate`)

Operator tools, for when you need them:

- pre-deployment checks of profile boundaries (`km-profile`)
- serialization of sync, index and MCP, with status and post-restore reconciliation (`python -m knowledge_memory.runtime`)
- retention planning and explicit deletion of sync evidence (`km-retention`)

Experimental:

- hybrid search with character n-grams or an optional embedding model (`km-hybrid`, opt-in; default search stays literal)
- an MCP entry point that enforces retrieval limits for evaluation (`km-eval-budget`)

## Quick start

Requires Python 3.11+ and SQLite FTS5 with the trigram tokenizer (SQLite 3.34 or later).
Only the `pip install` step downloads packages. The commands below use
only the synthetic data in this repository.

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'

.venv/bin/km validate --vault examples/vault
.venv/bin/km index --vault examples/vault --db .state/demo.sqlite3
.venv/bin/km search --db .state/demo.sqlite3 --scope demo 育種
.venv/bin/km get --db .state/demo.sqlite3 --scope demo note-search-example
```

The synthetic notes are written in Japanese; `育種` means "breeding".

### Five-minute demo

To run validate → index → search / get → context → MCP in one go (no external API, GPU or
credential; everything happens in a temporary directory that is removed afterwards):

```bash
.venv/bin/python examples/demo.py
```

See [docs/demo.md](docs/demo.md) for what each step shows and what it does not.

### Tests

```bash
.venv/bin/python -m unittest discover -s tests -v
```

CI runs on Python 3.11, 3.12 and 3.13.

## MCP

A stdio MCP server is included.

Default mode:

- `search_memory`
- `get_memory`
- `build_context`

Bounded mode:

- `search_memory_bounded`
- `get_memory_chunk`
- `build_context_bounded`

```bash
.venv/bin/km-mcp --vault examples/vault --db .state/demo.sqlite3 --scope demo
.venv/bin/km-mcp --vault examples/vault --db .state/demo.sqlite3 --scope demo \
  --bounded --response-max-chars 12000 --response-max-bytes 24000
```

The vault, database and scope are fixed when the server process starts. Candidates are
visible only when the server is started with `--include-candidates`; tool arguments from the
model cannot widen that. Arbitrary file reads and shell execution are not exposed as memory
tools. Bounded mode does not advertise the default tool names and never falls back to
full-text retrieval under those names. How to register the server differs by MCP client.

A working MCP connection does not mean an agent will choose to search and retrieve records on
its own. Check the SDK connection and, separately, whether your client actually searches, retrieves
the record and answers with sources. See "Checking that an agent actually uses the server" in
[docs/mcp.md](docs/mcp.md) and the [acceptance guide](docs/knowledge-acceptance.md).

## Record model

A record distinguishes at least:

- stable ID
- source
- retrieval / edit / event / verification time
- lifecycle status
- claim kind
- scope

`active` means "used by normal search"; it does not mean "true".

Details: [docs/schema.md](docs/schema.md)

## Notion ingestion

Nothing is written to Notion. Only read APIs are called (retrieving pages, blocks and data
sources, and querying data sources).

```text
Notion
  ↓  km fetch-notion (read-only API)
raw snapshot (outside the vault)
  ↓  km import-notion / km-sync
candidate Markdown (inside the vault)
  ↓  km index
search index (candidates are excluded by default; use --include-candidates explicitly)
  ↓
human review (making a record active is an explicit human step)
```

Imports do not treat incomplete pagination or inconsistent cursors as success.
Material that can no longer be fetched keeps its original text but is put on hold and removed
from search. Credentials, snapshots and real vaults are stored outside this repository.

## What it does not guarantee

- It does not verify whether source content is true
- `scope` is not authentication or authorization
- A search score is not a confidence value
- Zero hits do not prove that the information does not exist
- An MCP connection alone does not guarantee that an agent will use the memory
- The server cannot guarantee that an agent will not follow instructions contained in retrieved text; the client must treat it as data
- It is not a multi-user distributed database. It is designed for a single user and small vaults
- It is not a way to store a private vault on GitHub

## Verification status

- Automated tests and CI use synthetic fixtures and the official MCP Python SDK stdio client
- The evaluation results in this repository are for a synthetic corpus. They say nothing about search or answer quality on real data
- Split retrieval for very large Notion data sources (over the 10,000-item API limit) is not implemented
- Local-model use in `km-inbox` and embedding models in hybrid search come with no quality guarantee for any particular model

## Repository data policy

This repository contains synthetic fixtures only. It does not contain private vaults, imported
articles, raw snapshots, credentials, work data, real Notion IDs, or private evaluation
questions and results.

Reporting and contribution rules: [SECURITY.md](SECURITY.md), [CONTRIBUTING.md](CONTRIBUTING.md)

## Documentation

Several documents under `docs/` are still written in Japanese or mix Japanese and English.

Getting started

- [Five-minute demo](docs/demo.md) — the main path on synthetic data

Core design

- [Architecture](docs/architecture.md) — design decisions and functional boundaries
- [Record schema](docs/schema.md) — frontmatter, states, sources and times
- [MCP](docs/mcp.md) — tool contract, bounded mode, checking agent usage

Ingestion and operations

- [Notion import settings](docs/knowledge-import.md) and [conversion](docs/notion-conversion.md)
- [Synchronization](docs/sync.md) and [data source sync](docs/data-source-sync.md)
- [Acceptance](docs/knowledge-acceptance.md) — what to check after setting it up
- [Operations](docs/operations.md) — backup, restore, recovery checks, measurement
- [Inbox review](docs/inbox-review.md) — AI proposals and human approval

Advanced / experimental

- [Profile boundaries](docs/profile-boundaries.md) — keeping personal, work and public sources apart
- [Managed runtime](docs/managed-runtime.md) and [evidence retention](docs/sync-evidence-retention.md)
- [Hybrid search](docs/hybrid-search.md) — opt-in search comparison and evaluation

Maintainers

- [Releasing and updating](docs/releasing.md) — merging changes, release checklist, tags and GitHub Releases

## Status

v0.1, the first public release.
Clean install, security/privacy and accurate documentation take priority over new features.
Direction and contribution priorities: [docs/roadmap.md](docs/roadmap.md).

## License

[Apache License 2.0](LICENSE)
