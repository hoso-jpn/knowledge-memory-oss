# Inbox proposals and human review

Source files and the review SQLite database live outside this repository and outside the vault.
Run `python -m knowledge_memory.inbox` (or installed `km-inbox`):

```bash
km-inbox propose --source /outside/inbox.txt --vault /outside/vault --db /outside/review.sqlite3 --scope personal
km-inbox show --id proposal-ID --vault /outside/vault --db /outside/review.sqlite3
km-inbox revise --id proposal-ID --replacement /outside/correction.json --actor human --reason 'fix attribution' --vault /outside/vault --db /outside/review.sqlite3
km-inbox reject --id proposal-ID --actor human --reason 'duplicate' --vault /outside/vault --db /outside/review.sqlite3
km-inbox approve --id proposal-ID --actor human --reason 'checked exact source' --vault /outside/vault --db /outside/review.sqlite3
```

The default extractive-v1 adapter is a deterministic offline baseline, **not an LLM**. It
preserves the inbox as one source-report candidate and makes no semantic claims. The adapter
interface also supports an OpenAI-compatible local model:
`propose ... --endpoint http://127.0.0.1:8080/v1 --model local-model`.
This sends selected scope's active target texts and the inbox only to the operator-selected
loopback endpoint. It does not fetch source links, provide tools, execute commands, accept
model-selected paths, or follow redirects. User-configured forwarding proxies remain the
operator's responsibility. No cloud endpoint is accepted.

Model output must be bounded JSON with exact source quotation, attribution and operation.
Attribution maps user→user_statement, observation→observation, source→source_report,
ai→hypothesis. Schema validation is not truth verification. Inspect the exact quote and
attribution in `show`, including code/instructions contained in source text as data.

Propose is idempotent for the same source bytes, vault, scope, adapter identity and active
target hashes. If a target changes, proposing again generates a fresh review against the
new context while preserving the old proposal and its audit history. Raw bytes are retained
in the review DB. Rejected proposals are not regenerated under the same identity.
The ledger records original payload, corrections, actor, reason and timestamp. Never treat a
model's confidence as an approval. `show` returns a unified diff, including replace/append/
conflict suggestions. Revise cannot retarget or change operation; create a fresh proposal for
that change. `approve` refuses changed source or target hashes.

Approval creates a candidate by default. `--activate` is an explicit human decision to make
it active, not to assert verified truth. Append/replace of an active target requires this flag.
Append cannot mix attribution kinds or edit imported sources; create a linked note instead.
Replace atomically stages the old record as superseded and the new record with supersedes,
preserving the original body. Conflict creates a linked record without overwriting the target.
No verified_at is invented. The inbox is never moved or deleted; its approved/rejected state
is the processing record. Snapshots/evidence remain in the ledger after processing.

## Recovery and verification limits

Single writer to the vault; review DB serializes reviewers on the same ledger, but does not
coordinate unrelated editors or other ledgers. Hash checks detect edits before approval,
not arbitrary concurrent writes after the check. Filesystem writes and SQLite commit are
not one transaction: after a power loss inspect pending proposals and generated files before
retrying. Existing destinations and changed target hashes refuse blind reapplication.
Back up vault, original inbox and review DB together. Do not delete originals after approval.

Repository tests cover the extractive adapter, the HTTP adapter against a synthetic loopback
server, and every state transition. They do not measure a real model's extraction quality.
Before relying on a local model, run one named/versioned model on a synthetic inbox, record
the model identity, and judge attribution/new/append/replace/conflict quality separately.
When reviewing changes to this workflow, focus on injection-as-data, hash conflicts,
attribution mixing, approval recovery, original preservation, and why each active change is
authorized.

## Repeatable local-model acceptance run

Start an operator-selected OpenAI-compatible local server, then run from this checkout:

```bash
python -m knowledge_memory.inbox_evaluation \
  --cases evaluation/inbox-model-cases.json \
  --endpoint http://127.0.0.1:8080/v1 --model YOUR_MODEL_NAME \
  --model-revision YOUR_WEIGHTS_COMMIT_OR_SHA256 \
  > /outside/inbox-model-report.json
```

The five fixed synthetic cases cover all four attributions and new/append/replace/conflict,
including instruction-like text inside the source. The runner uses the real HTTP adapter,
validates exact source quotations, and compares the complete multiset of expected operation,
kind, attribution and target. Missing or extra proposals fail; it does not relax expectations
to improve a score. Without case selection, exit 0 means these automated checks passed; exit 2 means they did not
or the run was refused. The report records case-file hash, adapter/model identity, operator-
supplied weights revision, timestamp, timings and valid proposals. No vault or approval DB
is opened or modified. Keep custom private cases and reports outside this repository.

### Controlled requests to a running server

The evaluator accepts three optional controls; omitting them preserves the existing request
payload and continue-after-error behavior:

- `--max-tokens 512` sends `max_tokens: 512` in each HTTP request and records
  `generation_max_tokens_requested`. The value must be a positive integer and is included
  in the adapter identity. This requests a server-side generation limit; it does **not**
  prove that a particular server honors it. A response declaring `finish_reason: length` is
  rejected even if its JSON parses, and that rejection is recorded separately from a
  transport or schema failure, so the report can show which of the two happened.
- `--stop-on-error` stops after the first transport, response/schema, or exact-quotation
  validation error. It retains the failed row and prior rows, lists all unattempted IDs
  in `not_run_case_ids`, and never counts an incomplete suite as a pass. A valid response
  with a wrong classification remains a quality measurement and does not trigger this stop.
- `--case-id user-new` sends one request for that case while validating and hashing the
  **original full case file**. No derived single-case file or replacement scorer is needed.
  `selection_checks_passed` and exit 0 describe only the requested selection.
  `automated_checks_passed` remains false until every case in the input suite was attempted
  and passed in that run; `all_cases_attempted` and `not_run_case_ids` expose the difference.

Each row keeps what the response said about its own generation. `finish_reason` is recorded
only when it is one of `stop`, `length`, `tool_calls`, `function_call` or `content_filter`;
anything else is dropped and reported through `finish_reason_state`, which is `known`,
`unknown_value`, `invalid_type` or `absent`. A length cap on the string would not do, because a
short value is still text that came from the response. The `usage` counts are kept only for
non-negative plain integer token fields, so a boolean, a negative number or a float is dropped.
A row that failed also carries `failure_kind`, either `generation_truncated` or
`transport_or_schema`, and the report summarises `failure_kinds`, `finish_reasons` and
`finish_reason_states`. Without these a truncated run and a malformed one read the same.
Everything here is the server's own account of itself.

`generation_budget` records the comparison separately: the limit requested, the
`completion_tokens` the server reported, and an `observation` of `within`, `over` or
`not_observable`. It is deliberately not folded into `selection_checks_passed`, which is the
classification and quotation result and nothing else: a case can match its expected
classification while the server reports more tokens than were asked for, or reports no count at
all, and in both of those the checks still pass. An operator deciding whether to send the next
request reads `generation_budget`, not the checks. A server reporting a count is not the same
as a server enforcing a limit, and a server that reports nothing leaves the question open
rather than answered in its favour.

A `finish_reason: length` is refused whether or not a limit was requested. This differs from
the earlier behaviour, where a self-truncating server whose JSON happened to parse was
accepted: an answer built on a cut-off response is not grounded regardless of who set the
limit. The refusal records that the server declared a cut-off; it does not establish that the
server respected the figure that was asked for. Requests otherwise remain byte-identical when
the three controls are omitted.

For a constrained machine, select cases individually from the unchanged five-case file,
check resource/service conditions between requests, and save each report to a distinct new
path outside the vault. Fix the order and limits before running: at most five attempts,
one per original ID, no retries. Do not repeat the first case in a subsequent full-suite run.
Use each of the five original IDs exactly once when reporting combined coverage; keep the
original suite hash, per-case results, missing IDs, failures, and human review separate.
The CLI does not enforce a cross-process attempt ledger or resource thresholds: the operator
must retain that ledger and check the agreed conditions before each invocation.

```bash
set -o noclobber
python -m knowledge_memory.inbox_evaluation \
  --cases /outside/new-run/inbox-model-cases.json --case-id user-new \
  --endpoint http://127.0.0.1:8080/v1 --model YOUR_MODEL_NAME \
  --model-revision YOUR_WEIGHTS_COMMIT_OR_SHA256 \
  --max-tokens 512 --stop-on-error \
  > /outside/new-run/user-new.report.json
```

The endpoint and limits above are examples to fix for the actual run. The adapter uses a
60-second urllib socket timeout and a bounded response body. Neither is a hard wall-clock
deadline or a substitute for a generation-token limit. An external watchdog can stop the
client, but does not establish that the server canceled generation. If a timeout, resource
breach, or missing/invalid report occurs, preserve the attempt and stop further requests;
do not label an interrupted case as passed or infer that the server is idle.
Agree on an idle window and concrete resource thresholds before a live run, especially
when the endpoint has no readable queue status. Do not restart services or reload weights
to satisfy an evaluation. Tests of these controls use only synthetic loopback responses;
they do not verify a live server's token-limit enforcement or model provenance.

Human review is always required, even when all automated checks pass. Compare each proposed
body with its original inbox and targets: does it preserve the claim, correctly distinguish
the speaker from an AI deduction, justify the operation, and retain uncertainty/conflicting
evidence? Record pass/fail and the reason for each case alongside the report, together with
server version and model runtime options. An exact quotation alone does not prove that the
summary is supported. Follow the existing propose/show/revise/reject/approve workflow on
a separate synthetic vault before accepting a model for daily use. Neither this evaluator
nor its exit status authorizes an active record.

Repository tests exercise HTTP against a deterministic loopback fixture server. They do not
measure a real model's extraction quality; a named/versioned local LLM run remains required.
