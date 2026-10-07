# Recovery and operating cost

Operations use the read-only MCP adapter and core functions. They provide explicit commands,
never a daemon or schedule. Install `.[mcp]` and use `python -m knowledge_memory.operations`
or `km-ops`.

## Snapshot and rebuild

```bash
km-ops backup --vault /outside/vault --archive /outside/new-snapshot.zip
km-ops restore --archive /outside/new-snapshot.zip --target /outside/new-restored-vault
km index --vault /outside/new-restored-vault --db /outside/new-index.sqlite3
km-ops recovery --vault /outside/vault --scope personal --query '判断' --query '制約'
```

Stop all writers for a coherent backup. It preserves original Markdown bytes and, if present,
the sync availability ledger and selection registry, so that restored notes keep their
selection boundaries. SQLite indexes are disposable, so they are rebuilt. Review DBs, inbox
originals and raw sync evidence are separate assets: back them up too if you use inbox review
or sync. The ZIP manifest has per-file SHA-256/length and runtime versions. It is integrity
evidence, not an authenticated signature. Keep a trusted archive hash separately for tamper
detection.

Restore refuses existing targets, traversal paths, mismatched members/checksums and excessive
uncompressed size (128 MiB). It validates the entire staged vault before making it visible.
Never restore directly over a working vault. Compare original hashes and representative
queries first, then explicitly switch clients to the new fixed paths. Retain the old vault
until verification completes. A restore/rebuild cannot certify truth of source claims.

`recovery` round-trips the current vault through backup → restore → rebuild in a temporary
location and compares Markdown note bytes and the results of the given queries. The ledger and
registry travel with the archive but are not part of that byte comparison; check them yourself
when the vault is managed by sync. It does not detect that an older backup would re-serve sources
that have since been withdrawn; use `runtime reconcile` for that (see
[managed runtime](managed-runtime.md)). `recovery` and `probe-mcp` read active records only
unless `--include-candidates` is given.

## Measurement

`km-ops benchmark` creates only synthetic temporary notes at 25/250/1000 records, measures
index build, complete file hashing and search (five repetitions), then removes temporary data.
`evaluation/operations-synthetic-result.json` is one such run: environment, versions, counts,
bytes, raw timings, CPU/RAM information and measured RSS. CPU/RAM describe the host that ran
it, not necessarily a container allocation, and do not predict another machine.

`km-ops probe-mcp --vault /outside/vault --db /outside/index.sqlite3 --scope personal --query '線形'`
uses a real official SDK stdio client to initialize/list/search/get, compares source hashes and
closes the process. The report distinguishes MCP connected=true, cloud auto-connected=false,
and LLM-initiated=false. A connection that returns zero hits reports
`retrieval_verified: false`. This does not show that any LLM client chooses to search.

The synthetic report confirms byte-identical restoration and equivalent search results.
Tests cover candidate/retracted/other-scope exclusion and an absent-answer query after rebuild.
Zero hits mean no retrieval match, not an LLM abstention capability. Observation, edit time,
source reporting and hypotheses retain their existing metadata; no newest-is-true rule is added.

Full hashing is kept on every query. Small-corpus measurements do not justify the complexity
of a filesystem watcher. On your own machine, repeat with actual record sizes and distribution;
adopt incremental monitoring only if measured interactive latency exceeds an explicitly chosen
budget, while retaining periodic full checks to detect missed filesystem events.

### Measure an existing vault on its operating machine

Stop writers and first confirm that the existing index is current. The following command
reads the selected vault/index without rebuilding either or changing a note:

```bash
km-ops measure --vault /outside/vault --db /outside/index.sqlite3 \
  --scope personal --query '判断' --query '制約' --repeats 5 \
  > /outside/vault-performance.json
```

Use `--include-candidates` explicitly when evaluating imported candidates. The report records
total Markdown records/bytes, eligible records in the fixed scope/states, host/runtime
information, corpus fingerprint, full-hash samples and search samples/medians with hit counts.
It warms each query once; measured search still includes the normal full freshness check.
Hash timing also includes the small sync control files. Index build time is not measured by
this command. Query hashes (not text), counts and aggregate timings are written; source
bodies, note IDs and absolute vault paths are not included. Hashes are identifiers, not an
anonymization guarantee. Keep the original queries privately to reproduce a comparison.
The report also contains host details, so treat it as private operating evidence.

An index from another vault, a stale index or changed source/control bytes during the run
refuse a result, and the report records `index_rebuilt: false` / `vault_modified: false`.
These checks do not replace writer coordination. The command does not know which machine is
your target: run it there and record that host and its workload conditions alongside the
report. Do not generalize container timings to another machine. Choose an interactive
latency budget before comparing results, and include memory, local model storage/runtime and
any download costs separately. Peak RSS is `getrusage(RUSAGE_SELF).ru_maxrss` of the
measuring process, not the steady-state use of a long-running service.

## User effort study

No human time savings are claimed by this repository. To measure them, record matched real
tasks outside the repository, preferably alternating baseline/assisted order with comparable
task difficulty. Include all correction/organizing time, not only fast searches:

```json
[{"reexplain_seconds":120,"organize_seconds":100,"retrieve_seconds":30}]
```

Those numbers are an example, not measured user results. Run
`km-ops time-compare --observations /outside/observed-timings.json`.
It reports baseline minus (organize + retrieve), including negative savings. The command
rejects non-numeric entries but cannot tell a measured value from a guess; its `source` field
says the numbers were not automatically verified, so keep that label when reporting.
Record task, completion quality, date and tool/model versions separately. Do not fill missing
observations with zero or estimates.

API cost for the local synthetic run is zero; electricity and human time are unknown, not zero.
Dependency/model download and storage costs must be recorded separately when used.

## Multiple writers/devices: design before enabling

Current contract is one writer and serialized maintenance. Do not use bidirectional file sync
as a merge protocol. Before enabling more writers, implement a vault generation/revision,
compare-and-swap on expected hashes, an OS advisory lock shared across all mutations, and a
recoverable intent journal spanning files and metadata. Remote devices require a single owner
or an explicit conflict queue; last-writer-wins is unacceptable for approvals/retractions.
Source conflicts preserve both versions for human review. Do not promote pending candidates
or clear source holds during reconciliation. A separate implementation and failure-injection
review are required before claiming multi-writer support.
