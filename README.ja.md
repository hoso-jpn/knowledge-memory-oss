# knowledge-memory

[English](README.md) | **日本語**

> **Local-first, source-grounded memory infrastructure for AI agents.**

AIに長期記憶を与えながら、**出典・時点・状態・訂正履歴**を失わず、
人間による承認を境界に置くローカル知識基盤。

Markdownをsource of truthとし、SQLiteは再生成可能なindexとして扱う。
検索とMCPの基本経路はGPUや外部LLM APIを必要としない。

## Why

一般的な「AI memory」やRAGでは、検索された情報が次のように扱われやすい。

- いつの情報か分からない
- 何を根拠にしたか分からない
- 古い判断と新しい判断が混ざる
- AIが提案した内容が人間の確定事項へ混ざる
- 一度indexへ入った情報の削除・失効境界が曖昧
- private sourceとwork sourceが論理filterだけで混在する

`knowledge-memory` は検索精度だけでなく、**provenance / lifecycle / bounded retrieval / human approval**
を設計の中心に置く。

| 観点 | 一般的なRAG / vector store | Second Brain系のノートアプリ | knowledge-memory |
| --- | --- | --- | --- |
| 原本 | 埋め込み済みchunk | アプリ内のノート | Markdownファイル。indexは再生成可能 |
| 出典・時点 | 任意のmetadata | 任意 | activeは出典必須。編集・取得・出来事・確認の時点を別項目として持つ |
| AIの書き込み | 直接追加されやすい | 人が書く前提 | AI提案はcandidate。activeへの変更は人の明示操作 |
| 古い・誤った記録 | 上書きや削除 | 人が整理 | superseded / retractedとして残し、通常検索から外す |
| AIへの渡し方 | 上位k件 | 人がコピー | scope・状態で絞り、応答上限付きのMCPで返す |
| 意味検索 | 中心 | アプリ依存 | 既定は字面検索（FTS5 trigram）。意味検索は任意の実験機能 |

検索品質で汎用RAGに勝つことは目的にしていない。違いは、取得した記録が
「どこから・いつ・どの状態で」来たかをAIと人が確認できることにある。

## Core principles

- **Source-grounded** — active recordは1件以上のsource referenceを持ち、取り込んだrecordは取得時点を持つ
- **Local-first** — vaultとSQLite indexは利用者のマシンに置く。検索・原文取得・MCPは外部サービスへ接続しない
- **Rebuildable index** — DBを唯一の原本にしない
- **Human-in-the-loop** — AI生成候補はcandidateから始める
- **Explicit lifecycle** — candidate / active / superseded / retracted
- **Bounded context** — MCP responseとtask contextに明示的な上限を持たせる
- **Fail closed** — stale indexや不完全取得を成功として扱わない
- **Boundary aware** — personal/workはscopeだけでなくvault・DB・processを分けて運用する

## Architecture

```text
Notion / CSV / inbox text
          │
          ▼
   raw snapshot（vault外）
          │
          ▼
┌──────── Markdown vault ────────┐
│  candidate ──human review──► active
│  active ──► superseded / retracted
└────────────────────────────────┘
          │  km index（再生成可能）
          ▼
    SQLite FTS5 index
       │        │
       ▼        ▼
      CLI   stdio MCP ──► AI agent
```

candidateとactiveは同じvaultの中で `status` によって区別する。
Notionが原本の場合も、snapshotと派生Markdownを区別する。
SQLite indexはいつでもvaultから再生成できる。

## What it does

- YAML frontmatter validation（`km validate`）
- SQLite FTS5 trigram search と3文字未満の短語のliteral search（`km search`）
- state/scope aware search、stale-index detection
- source-grounded record retrieval（`km get`）
- task context generation（`km context`）
- read-only stdio MCP server と応答上限付きのbounded mode（`km-mcp`）
- Notion read-only fetch/import、CSV import（`km fetch-notion` / `km import-notion` / `km import-csv`）
- explicit synchronization and availability state（`km-sync`）
- candidate review workflow（`km-inbox`。既定は非LLMの抽出。任意でloopbackのローカルモデル）
- backup / restore / recovery check / measurement（`km-ops`）
- question-set evaluation（`km evaluate`）

運用者向けの補助（必要な場合だけ使う）：

- profile境界の配備前検査（`km-profile`）
- sync・index・MCPの直列化、状態、復元後の照合（`python -m knowledge_memory.runtime`）
- 同期証跡の保持計画と明示的な削除（`km-retention`）

実験的な機能：

- character n-gram / 任意の埋め込みモデルによるhybrid検索（`km-hybrid`、opt-in。既定の検索は字面一致のまま）
- 評価用に取得量の上限を強制するMCP entry point（`km-eval-budget`）

## Quick start

Python 3.11+ と、trigram tokenizerを含むSQLite FTS5（SQLite 3.34以降）が必要。
packageを取得するのは `pip install` のときだけ。以下はrepository同梱の架空データだけを使う。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'

.venv/bin/km validate --vault examples/vault
.venv/bin/km index --vault examples/vault --db .state/demo.sqlite3
.venv/bin/km search --db .state/demo.sqlite3 --scope demo 育種
.venv/bin/km get --db .state/demo.sqlite3 --scope demo note-search-example
```

### 5分demo

validate → index → search / get → context → MCPを一続きで試す（外部API・GPU・credential不要。
すべて一時directoryで行い、終了時に削除する）:

```bash
.venv/bin/python examples/demo.py
```

各stepが示すこと・示さないことは[docs/demo.md](docs/demo.md)（英語）を参照。

### Test

```bash
.venv/bin/python -m unittest discover -s tests -v
```

CIはPython 3.11 / 3.12 / 3.13を対象とする。

## MCP

stdio MCP serverを利用できる。

通常mode:

- `search_memory`
- `get_memory`
- `build_context`

bounded mode:

- `search_memory_bounded`
- `get_memory_chunk`
- `build_context_bounded`

```bash
.venv/bin/km-mcp --vault examples/vault --db .state/demo.sqlite3 --scope demo
.venv/bin/km-mcp --vault examples/vault --db .state/demo.sqlite3 --scope demo \
  --bounded --response-max-chars 12000 --response-max-bytes 24000
```

server process起動時にvault / DB / scopeを固定する。candidateの参照は起動時の
`--include-candidates` でだけ許可し、モデルのtool引数では広げられない。
任意file readやshell executionはmemory toolとして公開しない。
bounded modeは旧tool名を広告せず、旧名による全文取得へfallbackしない。
MCP clientへの登録方法はclientごとに異なる。

MCPに接続できても、agentが自分から検索・原文取得を選ぶとは限らない。
SDKによる接続確認と、実際のclientで検索→get→出典付き回答が起きるかの確認は別に行う。
手順は[docs/mcp.md](docs/mcp.md)の「Checking that an agent actually uses the server」と
[導入と受け入れ](docs/knowledge-acceptance.md)を参照。

## Record model

recordは少なくとも次を区別する。

- stable ID
- source
- retrieval/edit/event/verification time
- lifecycle status
- claim kind
- scope

`active` は「通常検索で利用可能」という意味であり、「真実」と同義ではない。

詳細: [docs/schema.md](docs/schema.md)

## Notion ingestion

Notionへは書き込まない。呼び出すのは読み取り用のAPI（page・block・data sourceの取得とquery）だけである。

```text
Notion
  ↓  km fetch-notion（read-only API）
raw snapshot（vault外）
  ↓  km import-notion / km-sync
candidate Markdown（vault内）
  ↓  km index
search index（candidateは既定で検索対象外。--include-candidatesで明示利用）
  ↓
human review（activeへの変更は人の明示操作）
```

import処理は不完全なpaginationや矛盾したcursorを成功扱いしない。
取得できなくなった資料は、原文を残したまま利用保留にして検索から外す。
credential、snapshot、実vaultはrepository外に保存する。

## What it does not guarantee

- source contentの真偽を自動検証しない
- `scope` はauthentication / authorizationではない
- search scoreはconfidenceではない
- 0 hitsは「情報が存在しない」ことの証明ではない
- MCP接続だけでagentが必ずmemoryを使うことを保証しない
- 取得した本文の中の命令をagentが実行しないことは、server側では保証できない。client側でdataとして扱う
- multi-user distributed databaseを目的としていない。単一利用者・小規模vaultを想定する
- private vaultそのものをGitHubへ保存する仕組みではない

## Verification status

- 自動testとCIはsynthetic fixtureと公式MCP Python SDKのstdio clientで行う
- repository内の評価結果はsynthetic corpusに対するもの。実データでの検索・回答品質は示していない
- Notion取り込みの大量データ（API上限10,000件超）の分割取得は未実装
- `km-inbox` のローカルモデル利用、hybrid検索の埋め込みモデルは、特定モデルでの品質を保証しない

## Repository data policy

このrepositoryにはsynthetic fixtureだけを含める。
private vault、imported articles、raw snapshot、credential、work data、実Notion ID、
private評価の質問・結果は含めない。

報告と貢献のルール: [SECURITY.md](SECURITY.md)、[CONTRIBUTING.md](CONTRIBUTING.md)

## Documentation

`docs/` には日本語・英語・両者の混在した文書がある。

Getting started

- [5分demo](docs/demo.md) — synthetic dataで主要経路を試す（英語）

Core design

- [Architecture](docs/architecture.md) — 採用した設計判断と機能の境界
- [Record schema](docs/schema.md) — frontmatter、状態、出典と時点
- [MCP](docs/mcp.md) — tool契約、bounded mode、agentの利用確認

Ingestion and operations

- [Notion import settings](docs/knowledge-import.md) と [conversion](docs/notion-conversion.md)
- [Synchronization](docs/sync.md) と [data source sync](docs/data-source-sync.md)
- [Acceptance](docs/knowledge-acceptance.md) — 導入後に確認すること
- [Operations](docs/operations.md) — backup、restore、復旧確認、測定
- [Inbox review](docs/inbox-review.md) — AI提案と人の承認

Advanced / experimental

- [Profile boundaries](docs/profile-boundaries.md) — 個人・業務・公開用の分離
- [Managed runtime](docs/managed-runtime.md) と [evidence retention](docs/sync-evidence-retention.md)
- [Hybrid search](docs/hybrid-search.md) — opt-inの検索比較と評価方法

## Status

v0.1。最初の公開版である。
機能追加よりも、clean install、security/privacy、documentationの正確さを優先している。

## License

[Apache License 2.0](LICENSE)
