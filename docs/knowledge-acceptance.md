# Knowledge Databaseの導入と受け入れ

Notion Knowledge DatabaseをCLI・MCPから使うまでの導入手順と、導入後に何を確認すべきかをまとめる。
repositoryのtestとCIはsynthetic fixtureで行う。利用者のデータ・クライアント・モデルでの受け入れは、
この手順に沿って利用者が別に記録する。

設定と固定質問を用意した後は[受け入れ記録コマンド](live-acceptance.md)で、別の新規保存先へ
dry-run→sync→再sync→index→検索/get→任意のSDK probeをまとめて記録できる。
本文や回答の人による確認、実AIクライアント自身のツール選択は引き続き別に評価する。

## 1. 初回取得からCLI参照

[取り込み設定](knowledge-import.md)のRead content IntegrationをKnowledge DatabaseのConnectionsへ追加する。
対象はそのデータソース全体であり、将来の件数を固定しない。Blog用の設定をコピーしてContent/Slugを要求しない。

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[mcp]'
KM_ROOT="$HOME/knowledge-data/knowledge"
KM_DATA_SOURCE_ID='<利用者のdata_source_id>'
mkdir -p "$KM_ROOT"
cp examples/config/knowledge-import.yaml "$KM_ROOT/import.yaml"
read -rs NOTION_TOKEN && export NOTION_TOKEN
.venv/bin/km fetch-notion --data-source "$KM_DATA_SOURCE_ID" \
  --out "$KM_ROOT/snapshot" --vault "$KM_ROOT/vault"
.venv/bin/km import-notion --snapshot "$KM_ROOT/snapshot" --vault "$KM_ROOT/vault" \
  --scope personal-knowledge --import-config "$KM_ROOT/import.yaml"
.venv/bin/km index --vault "$KM_ROOT/vault" --db "$KM_ROOT/index.sqlite3"
.venv/bin/km search --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge '<原本にある語>'
.venv/bin/km search --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge --include-candidates '<原本にある語>'
```

`KM_ROOT`はrepositoryの外に置く。通常検索はcandidateを除外するので、初回資料だけなら0件になる。
後者で資料を明示利用する。0件を理由に一括activeへ昇格しない。整理状態も承認・検証を意味しない。
候補検索で返った安定IDを使って原文を取得し、内容・表・出典を原本と突き合わせる。

```bash
KM_NOTE_ID='<検索で返ったsrc-ページID>'
.venv/bin/km get --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge \
  --include-candidates "$KM_NOTE_ID" > "$KM_ROOT/source-check.json"
.venv/bin/km context --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge \
  --include-candidates --goal '<確認したい目的>' --budget 8000 --out "$KM_ROOT/context.md"
```

現在の検索は字面一致。自然文をそのままCLIに渡して0件でも資料が無い証拠にはならない。
表の検索hitからgetで表全体を読み、対応する行・列と見出しを確認する。
長いcontextでtruncated/quote_omitted/droppedがあれば、表示されたIDのgetで末尾まで補う。
同梱の人工データでは、2600文字の予算で末尾の表が省略され、明示マーカーとgetで補完できることをtestしている。

結果の確認項目：metadata.status=candidate、claim_kind=source_report、origin.source_url、
origin.retrieved_at、source_edited_at、source_metadata.document_date、data_source_id。
contextにも取得時点・原本編集時点・資料日を表示する。資料が何を述べたかと最新仕様の検証を区別する。
本文中の要約・公式補足・適用案などの節や、資料版・Web確認日はそのまま引用し、混ぜて一つの事実にしない。

## 2. 明示syncと切り分け

[data-source-sync.md](data-source-sync.md)の設定をKM_ROOTへ置き、別selection_id/stateでBlogと分ける。
Notionで通常の追記が起きた後、手動でsync→index→同じ検索/getを実行する。
疎通確認のために実ノートへダミー文を追加しない。既存の編集を読み取るか人工fixtureを使う。

```bash
.venv/bin/km-sync --config "$KM_ROOT/knowledge-sync.yaml" --vault "$KM_ROOT/vault" --state "$KM_ROOT/sync-state"
.venv/bin/km index --vault "$KM_ROOT/vault" --db "$KM_ROOT/index.sqlite3"
```

| 症状 | 確認と対応 |
| --- | --- |
| 401/403/404、未接続 | CLIトークンのRead content権限とDB Connections。別アプリの接続成功で代用しない。404は削除の証拠ではない |
| 索引が古いというエラー | 原文が更新・追加・削除された。sync結果を確認してindexを再構築する |
| 通常検索だけ0件 | candidate除外を確認。明示候補利用で比較し、statusは変えない |
| 候補利用でも0件 | scope・字面の検索語・保留ledger・取得/変換レポートを確認する |
| needs_body_choice/needs_review/型の不一致 | 設定と原本の型、手編集/昇格状態、conversion_reportsを確認する |
| DS列挙失敗/移動/archived/404 | ノートは保存されるが利用保留。候補利用・履歴・古い索引で迂回しない |
| resume拒否 | 設定・vault・selectionの一致と未完了runを確認。DSは再列挙する |

## 3. ローカルMCPクライアント

起動設定のvault/DB/scope/候補利用だけが権限を決める。
モデルがtool引数でinclude-candidatesや別scopeを要求して広げることはできない。
`.venv/bin/km-mcp`の絶対パスをローカルクライアントのstdioサーバーへ登録し、次の引数を固定する。

```text
--vault /absolute/path/knowledge/vault
--db /absolute/path/knowledge/index.sqlite3
--scope personal-knowledge
--include-candidates
```

具体的な登録と確認方法は[MCP接続手順](mcp.md)。モデルのMCPへNOTION_TOKENは不要。
クライアントの名前と版を記録し、プロジェクト指示（`CLAUDE.md`経由の`AGENTS.md`等）が読まれたことを
そのクライアントの機能で確認する。CLI/MCPの導入とクラウドUIへの自動接続は別である。

まずSDK経路だけを切り分ける（この結果は実LLMクライアントの受け入れとは別）：

```bash
.venv/bin/km-ops probe-mcp --vault "$KM_ROOT/vault" --db "$KM_ROOT/index.sqlite3" \
  --scope personal-knowledge --include-candidates --query '<原本にある語>'
.venv/bin/km-ops recovery --vault "$KM_ROOT/vault" --scope personal-knowledge \
  --include-candidates --query '<原本にある語>'
```

probeのmcp_connectedとretrieval_verifiedを分けて見る。0件ならretrieval_verified=false。
候補参照は起動オプションでのみ許可。復元は同期のselection登録簿も保存し、DB間の境界を維持する。
実運用の復元はwriterを止め、既存vaultを上書きしない[復旧手順](operations.md)に従う。

## 4. 実データ・実クライアントでの受け入れ

repositoryのtestはこの節を代替しない。原本と対応見出しを実際に読んでから期待値を固定し、
質問を自然文でクライアントへ渡す。検索tool選択→get→根拠箇所の引用を実行ログで確認する。

質問は次の型を含めると、取りこぼしの種類を切り分けやすい。

| 質問の型 | 確認する焦点 |
| --- | --- |
| 1つの資料に答えがある | 原本見出しと資料版。要約と原文を取り違えない |
| 2つの概念の違い | 両方の記録の原文取得と根拠見出し |
| 複数ノート横断 | 必要な記録を全部取得したか。`min_hits`で期待件数を固定する |
| 表・比較表 | 行と列の対応と出典 |
| ノート末尾の表・公式出典 | context省略表示からgetで末尾を補えるか |
| 未収録・古い版しかない・取得不能 | 出典を捏造せず、取得できた範囲と資料時点を示すか |

repository外の受け入れ記録に、実行日、クライアント名/版、モデル名、導入commit、scope、候補設定、
安定ID、原本URL、見出し/locator、資料日/編集日時/取得日時、実際のtool順、合否、未確認点を書く。
本文・回答全文・トークン・生成DBはcommitしない。公開する場合は匿名化した合否と制約だけにする。
自然言語への回答品質や、モデル自身が適切な検索語を選ぶかは、SDKテストでは証明されない。

## 5. 再現可能な人工評価

`examples/knowledge-notion`は完全な架空4資料。
1件を意図的に長くし、資料日が古い例、表、添付控え、横断検索、未収録質問を含めた。

```bash
KM_DEMO_ROOT="$(mktemp -d)"
.venv/bin/km import-notion --snapshot examples/knowledge-notion --vault "$KM_DEMO_ROOT/vault" \
  --scope demo --import-config examples/config/knowledge-import.yaml
.venv/bin/km index --vault "$KM_DEMO_ROOT/vault" --db "$KM_DEMO_ROOT/index.sqlite3"
.venv/bin/km evaluate --db "$KM_DEMO_ROOT/index.sqlite3" --scope demo --include-candidates \
  --questions evaluation/knowledge-synthetic-questions.yaml
.venv/bin/python -m unittest discover -s tests -p test_knowledge_acceptance.py -v
```

記録済みの結果は `evaluation/knowledge-synthetic-result.json`（6検索hit、未収録1件は0件、miss/false_hitとも0）と、
`evaluation/knowledge-mcp-sdk-result.json`（SDK接続とgetの一致のみ）にある。
CLI subprocess＋公式SDKの6テストで、4資料の原文/metadata、反復、状態とscope、末尾補完、再index、
同期保留を既存MCPセッションから迂回できないこと、candidateの復元とselection登録簿を確認する。
意味検索は必須にしない。実質問で失敗を再現してから、検索改善の要否を判断する。

クライアント操作の参照元：
[Claude Code MCP](https://code.claude.com/docs/en/mcp)、
[プロジェクト指示の読込](https://code.claude.com/docs/en/memory)。
