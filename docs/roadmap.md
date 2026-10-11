# Roadmap and contribution priorities

This page describes the direction of the public project for roughly the next three to six months
and where contributions help most. It is a direction, not a commitment: items can change order,
change shape or be dropped, and none of them has a release date. Concrete work is tracked in
[Issues](https://github.com/hoso-jpn/knowledge-memory-oss/issues). If something here and an
Issue disagree, the Issue is more current.

The design boundaries in [AGENTS.md](../AGENTS.md), [CONTRIBUTING.md](../CONTRIBUTING.md) and
[SECURITY.md](../SECURITY.md) apply to everything below. Nothing on this page relaxes them.

## Completed foundation

Released in `v0.1.0`:

- Markdown vault as the source of truth with a rebuildable SQLite FTS5 index
- explicit candidate / active / superseded / retracted lifecycle and source/time provenance
- read-only stdio MCP server, with a bounded mode that limits each response
- read-only Notion and CSV ingestion into candidates; human review through `km-inbox`
- backup, restore and recovery checks; profile boundary checks

Done on `main` since `v0.1.0`:

- CI builds the wheel and sdist once and installs each into a clean venv on Python 3.11–3.13,
  with a contents check of both artifacts (#1)
- a five-minute end-to-end demo on synthetic data, covered by the test suite ([docs/demo.md](demo.md), #4)
- an English-first README with a Japanese version (#3)
- a maintainer guide for merging and releasing ([docs/releasing.md](releasing.md), #2)

## Current priorities

These are the areas the maintainer expects to work on first.

### Packaging and distribution hardening

Tighten the artifact checks added in #1 and prepare the metadata a package registry would
need, without publishing anything yet. The open items are collected in #8: dependency metadata
and RECORD checks, package long description, installs with the `[mcp]` and `[semantic]` extras,
and macOS / Windows coverage. Publishing to PyPI would be a separate, explicit decision.

### English documentation

The README is English-first, but several documents under `docs/` are still in Japanese or mix
both languages. The goal is an English version of each, starting with the documents a new user
reads first: [schema](schema.md), [architecture](architecture.md) and [MCP](mcp.md).
Translations must keep every limitation and caveat of the original.

### Examples and demo

The demo uses a small Japanese fixture. Useful additions are a small English synthetic vault and
short walkthroughs for common tasks (reviewing a candidate, retracting a record, building a
context for a project), each runnable without credentials and covered by a test.

## Future exploration

These need measurement or design before anyone commits to them.

- **Larger-vault measurement.** `km-ops benchmark` currently measures synthetic vaults of
  25 / 250 / 1000 records ([operations](operations.md#measurement)). Larger synthetic sizes and
  more realistic note-size distributions would show where full hashing on every query or index
  rebuilds become too slow, before changing either.
- **Semantic / hybrid retrieval evaluation.** `km-hybrid` is opt-in and no pretrained model is
  evaluated in this repository ([hybrid search](hybrid-search.md#not-verified)). A reproducible
  evaluation on synthetic questions, including CPU and memory cost, would come before any change
  to the default literal search.
- **MCP interoperability.** The server is tested with the official Python SDK stdio client.
  Recording how specific MCP clients and bridges behave (tool listing, argument handling,
  response limits) would help users, as long as a passing check is reported for that client and
  version only ([MCP](mcp.md)).
- **Recovery and retention usability.** Backup, restore, recovery checks and sync-evidence
  retention work but are documented for operators. Clearer messages, dry-run output and short
  guides would make them easier to use correctly.

### Not planned without a separate design discussion

Changes like these would weaken a security or privacy boundary and are not on the roadmap. They
would need their own Issue, a design and an explicit review first:

- promoting candidates to active automatically, or letting MCP tools write to the vault
- letting the model choose the vault, database, scope or candidate access through tool arguments
- sending vault content to cloud model endpoints by default (`km-inbox` accepts loopback only)
- multiple writers or devices on one vault (see the requirements in
  [operations](operations.md#multiple-writersdevices-design-before-enabling))
- treating `scope` as authentication or authorization

## Where contributions help

Good starting points that need no private data and little background:

- **Documentation**: translate one Japanese document under `docs/` into English, or fix a
  command or explanation that no longer matches the code. Keep the caveats.
- **Tests**: add a synthetic fixture that reproduces an edge case (frontmatter validation, search
  filters, context budgets, bounded MCP chunking) together with the test.
- **Examples**: a small synthetic vault or walkthrough, as described above, with a test that
  runs it.
- **Benchmarks**: extend the synthetic measurement and report results with the environment that
  produced them; numbers from one machine do not predict another.
- **Packaging**: the checklist in #8.

Issues labelled
[`good first issue`](https://github.com/hoso-jpn/knowledge-memory-oss/labels/good%20first%20issue)
or [`help wanted`](https://github.com/hoso-jpn/knowledge-memory-oss/labels/help%20wanted) are
the clearest places to start when they exist. For anything larger, open an Issue first so the
approach can be agreed before the work. Follow [CONTRIBUTING.md](../CONTRIBUTING.md): synthetic
data only, one purpose per PR, and state which tests you ran.
