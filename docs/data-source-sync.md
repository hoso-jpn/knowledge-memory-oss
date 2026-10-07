# データソース単位の手動同期

[`km-sync`](sync.md)を拡張する。別の同期エンジンは作らない。Notionを編集原本とし、
選んだデータソース全体だけをAPIで列挙する。Notionの画面のビュー/フィルターは使わない。
新しいページは同じ設定でcandidate/source_reportとして追加される。

## 利用者側設定

リポジトリ外の `knowledge-sync.yaml` と同じディレクトリに、[取り込み設定](knowledge-import.md)の `import.yaml` を置く。
実DB/data source IDは利用者側のこのファイルへ記入する。

```yaml
selection_id: personal-knowledge
scope: personal-knowledge
sources:
  - provider: notion
    data_source_id: '00000000-0000-0000-0000-000000000001' # 利用者のIDへ置換
    import_config: import.yaml
```

page_idを指定する従来のNotion選択とGitHub repo/path/refも引き続き利用できる。
import_configは相対/絶対ファイルパスで、内容をチェックポイントの設定指紋に含める。
従来のbody_source/body_property/title_property/page_body_slugsも使用できるが、import_configとの競合は拒否。

```bash
# Read contentだけのIntegrationを対象DBのConnectionsに追加する。
# ChatGPT側のNotion接続とは別の権限確認。
read -rs NOTION_TOKEN && export NOTION_TOKEN
KM_ROOT="$HOME/knowledge-data/knowledge"
km-sync --config "$KM_ROOT/knowledge-sync.yaml" --vault "$KM_ROOT/vault" \
  --state "$KM_ROOT/sync-state" --dry-run
km-sync --config "$KM_ROOT/knowledge-sync.yaml" --vault "$KM_ROOT/vault" \
  --state "$KM_ROOT/sync-state"
km index --vault "$KM_ROOT/vault" --db "$KM_ROOT/index.sqlite3"
km search --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge --include-candidates '検索語'
```

初回は `km fetch-notion` / `km import-notion` から始めてもよい。page IDを維持し、origin.data_source_idが一致する
既存ノートを同期へ引き継ぐ。古いsnapshotに由来がない場合は再取得・再importしてから移行する。
業務/個人のscope・保存先分離を保つ。実データ・トークン・DB・証拠をコードへコミットしない。

## 同期の境界と再開

- BlogとKnowledgeは異なるselection_idとstateディレクトリを使う。同じvault/scopeでも他のselectionをunselectedにしない。
- 同じselection_idに複数DSを登録した後、一部だけの設定で実行すると拒否する。意図的なDS登録解除のCLIは未実装。
- 同じページを別selectionで管理することは拒否する。同じselection内のpage指定＋DS指定は設定一致なら1回、
  設定競合なら利用保留。DB取得失敗をpage指定で迂回させない。
- 同期前に由来が分かる既存ノートをpendingへ保留し、正常に検証・保存できたものだけcurrentへ戻す。
- 完了した列挙にだけ所属欠落の判断を許す。0件は正常。欠落ページを個別確認し、moved/archived/
  missing_or_inaccessible/missing_from_data_source等を区別する。404だけで削除と断定しない。
- schema/一覧取得が失敗したDSは既知の所属ノートを利用保留にする。他のDSは処理を継続する。
- `--resume`は同じ設定・vault・selectionの未完了runだけ。DSは再列挙し、所属ページを再確認する。
  保存先の記録がない旧チェックポイントは再開せず、通常の新規runで取得し直す。
  不一致はディレクトリやlockを作成する前に拒否し、処理開始時にも再確認する。
  完了ページもDSでは再取得するため中断時の古い所属一覧を信用しない。個別sourceは成功済みをskipする。
- チェックポイントには設定指紋・列挙完了フラグ・ページ集合・証拠hash・時点、ledgerにはページ所属を保持。
  raw応答と列挙証拠はstate以下へ保存する。ページ単位の取得日時なので全体の原子的snapshotではない。
- APIのquery上限10,000件に到達した場合は不完全の可能性があるため停止する。大規模分割列挙は未実装。
- dry-runは取得・比較だけ。vault、snapshot、ledger、チェックポイント、registryを一切変更しない。
- dry-runも列挙失敗や取得不能があれば終了コード2。書き込まないことは正常終了と同義ではない。

`complete`は取得runの完了を表し、ノートの承認ではない。needs_reviewや欠落のsources報告も確認する。
手編集/昇格済みノートを上書きせず、原本を残す。ledgerは検索/get/contextの各読み取り時に確認するので、
再index前でも既存索引から保留資料を返さない。更新されたMarkdownの参照には再indexが必要。

初回importのabsent_from_snapshotも同じscope＋origin.data_source_id（無ければdatabase_id）に限定した。
曖昧で保留したページは所属として数え、別DBや所属不明のノートを欠落扱いにしない。
completeな空snapshotも受け付ける。由来なしsnapshotはabsence_checked=false。import単独はledger同期の代用ではない。

単一writer、実行時例外のファイルrollbackという[`km-sync`](sync.md)の前提を継承。電源断や外部エディタとの同時更新は保証外。
同期lockが残ったらwriterの停止と原本・stateを確認し、未完了runを再開する。
原本参照不能からの手動解除やDS登録解除は、証拠確認を伴う別の操作として今後設計する。

[Notion query仕様](https://developers.notion.com/reference/query-a-data-source)（2026-09-26確認）。
人工データで100件超・0件・cursor異常・429・中断・移動/archived/404・権限喪失を検証。
repositoryのtestは実Notion APIへ接続しない。自分のdata sourceでの接続と受け入れは[導入手順](knowledge-acceptance.md)に沿って確認する。
