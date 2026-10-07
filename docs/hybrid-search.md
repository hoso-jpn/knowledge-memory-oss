# Opt-in retrieval comparison

The default `km search` and `km context` remain literal. Experimental `km-hybrid` offers an
additional retrieval path without claiming that similarity is truth or that zero hits prove
no evidence exists. Literal hits keep their ordering and take priority; exact IDs are also
looked up. Candidate/history/scope rules and returned sources are inherited from core.

```bash
km index --vault /outside/vault --db /outside/index.sqlite3
km-hybrid index --db /outside/index.sqlite3 --vectors /outside/vectors.sqlite3 --scope personal
km-hybrid search --db /outside/index.sqlite3 --vectors /outside/vectors.sqlite3 --scope personal --query '線形モデル'
km-hybrid compare --db /outside/index.sqlite3 --vectors /outside/vectors.sqlite3 --scope personal --questions /outside/questions.yaml
```

`python -m knowledge_memory.hybrid` is the same entry point. `--threshold` (default 0.2) and
`--include-candidates` apply to search and compare.

Default encoder is **character-ngram-v1**, a dependency-free lexical control, not a semantic
pretrained model. It uses 512-dimensional deterministic hashed character bigrams/trigrams.
For semantic embeddings install `.[semantic]` (sentence-transformers==5.1.2), acquire a chosen
model in a separate explicit download step, then add `--local-model /outside/model-snapshot`
to both index and search/compare. The directory must already exist. Loading uses CPU,
local_files_only=True, offline environment flags and trust_remote_code=False. There is no
automatic model download, GPU requirement, remote inference or executable-code trust.
Do not use an unreviewed local model package; record its upstream repository, exact revision,
license and download hash in your external model manifest.

The vector header records backend/package, weight-file fingerprint, source hashes, scope,
chunk size (256 Unicode characters / 32 overlap), index version and dimensions. Query/document
encoders use their respective prompts. Over-token-budget input fails instead of silently
truncating. Any model/notes/index mismatch requires full rebuild. Updating/deleting a note
invalidates vectors, and rebuilding replaces all derived rows atomically. No pickle is loaded.
The model is fingerprinted once when the process starts; restart after changing model files.

## Synthetic experiment

`evaluation/hybrid-synthetic.json` is a six-question synthetic set over the demo vault.
The frozen run at threshold 0.2, limit 10 is in `evaluation/hybrid-character-result.json`.
It contains only synthetic IDs and results.

- Literal success: 3/6. Character hybrid success: 4/6.
- Improvements: exact ID and one natural-language question.
- Regression: the absent-answer question acquired false hits (0→1).
- A constraint question still failed. No questions/threshold were retuned after this result.
- Literal and short-query hit ordering remained intact.
- 16 records across all demo states, 24 chunks. Build and query times were milliseconds and
  peak RSS about 20 MiB on the recording machine. Such tiny synthetic timings do not predict
  performance on another machine or a larger vault.

Therefore the control is **not adopted as the default**. Additional recall is not enough to
justify false evidence. Reranking has not been added. Compare a chosen semantic model on your
own unchanged evaluation set and a separate holdout before deciding whether it is needed.

## Comparing methods

`compare` reports per-question reciprocal rank, false hits, improvements/regressions,
timing and CPU RSS. It accepts the same YAML `questions` mapping as `km evaluate`, including
`expect: unknown` and `min_hits`. Multi-record questions pass only when the required number
of distinct expected IDs is returned. The original JSON list remains supported (empty
`expect_ids` means unknown). `matched_expected` and `min_hits` are reported per method.

Rules that keep a comparison honest:

- Freeze questions, expected IDs, `min_hits`, the candidate setting and the threshold before
  running. Do not rewrite questions or relax `min_hits` to make a new method pass.
- Keep a holdout set that was not used to design the change, and report it separately from
  sets whose results you have already seen. A re-run on seen sets is not evidence of
  performance on unseen questions.
- Report false hits on questions whose expected answer is "not recorded". A threshold that
  improves recall but returns candidates there is a regression, not a neutral change.
- Zero hits from either path only mean "not found by this search". Similarity search can
  also return nothing above its threshold.
- Retrieval success and answer success are different stages. An answer can omit a record that
  retrieval returned; improving retrieval does not fix that. Do not combine the two into one
  improvement figure.

## Evaluating answers under an enforced budget

A retrieval limit written in a prompt is a request, not a limit. `km-eval-budget` is an
evaluation-only MCP entry point that enforces a per-question budget on the path that returns
tool results:

```bash
km-eval-budget --vault /outside/vault --db /outside/index.sqlite3 --scope personal \
  --tools search_memory,get_memory --max-searches 6 --max-documents 3 \
  --max-characters 40000 --budget-report /outside/run/q01-budget.json
```

- `search_memory` spends a search; `get_memory` and `build_context` each spend a document.
- Every Unicode character of the JSON returned by any tool spends characters (`len`, not
  bytes). A repeated read is charged again.
- A result that does not fit is refused, never shortened. A refused call still spends its slot.
- Only the tools named in `--tools` are listed and callable.
- `km-mcp` is unchanged; this entry point is for evaluation harnesses only.

`knowledge_memory.eval_harness` scores the resulting sessions by fixed rules:

- A session is classified (`session_outcome`) before it is scored. A session that never
  reached the model, for example after HTTP 429, cites nothing; on a "not recorded" question
  that would otherwise score as a success, so such sessions cannot be scored.
- Every attempt counts toward a run's request cap, including retries.
- The answer's grounds line is classified as `ids`, `empty`, `missing` or `malformed`. Only a
  bare `none` declares "no grounds". A missing or malformed line is not read as "no citation".
  Tokens are validated whole; `src-x_` is not truncated to `src-x`.
- Budget reports must contain searches, documents and characters for both limits and spend,
  as non-negative integers (not booleans). A missing value is not read as zero. Only a session
  with no tool calls may lack a report.
- Where the harness performs the first retrieval itself, totals are reported as harness plus
  server.
- "Matched" means the cited IDs mechanically matched the expected IDs. It is not a judgment
  of answer quality; whether the prose follows the cited record needs human review.

## Not verified

- No pretrained semantic model is evaluated in this repository. The local adapter path and
  metadata checks are implemented and tested; model quality, network-disabled inference and
  CPU/memory cost depend on the model you choose.
- Results on real corpora are not included. Keep private questions, logs and results outside
  the repository.
- GPU use is not measured; both encoders request CPU.

Model loading reference:
https://www.sbert.net/docs/package_reference/sentence_transformer/model.html
