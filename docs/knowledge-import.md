# Knowledge Databaseの取り込み設定

Notion本文が編集原本。添付MDは保存時点の控えで、自動取得・結合しない。
`examples/config/knowledge-import.yaml`をリポジトリ外へコピーし、列名を対象DBへ合わせる。
この設定は `version: 1 / body_source: page` と8個の明示的なプロパティ対応を持つ。
列名の誤記・型の相違は失敗させ、別の日付・multi_selectを代用しない。
本文・title・slugの従来CLI指定との併用は拒否する。設定なしのブログ経路は従来通り。

```bash
# 利用者自身の値を設定する。ID・scope・保存先はライブラリへ埋め込まない。
KM_DATA_SOURCE_ID='<Knowledgeのdata_source_id>'
KM_ROOT="$HOME/knowledge-data/knowledge"
mkdir -p "$KM_ROOT"
cp examples/config/knowledge-import.yaml "$KM_ROOT/import.yaml"
# Read content権限のみのIntegrationを対象DBのConnectionsへ追加してから実行。
read -rs NOTION_TOKEN && export NOTION_TOKEN
km fetch-notion --data-source "$KM_DATA_SOURCE_ID" --out "$KM_ROOT/snapshot" --vault "$KM_ROOT/vault"
km import-notion --snapshot "$KM_ROOT/snapshot" --vault "$KM_ROOT/vault" \
  --scope personal-knowledge --import-config "$KM_ROOT/import.yaml"
km index --vault "$KM_ROOT/vault" --db "$KM_ROOT/index.sqlite3"
km search --db "$KM_ROOT/index.sqlite3" --scope personal-knowledge --include-candidates '検索語'
```

Blog用のsnapshot/vault/DB/設定と混同しない。業務資料は別scope・別保存先を使う。
ChatGPTのNotion接続は、このCLIのNOTION_TOKENの権限を保証しない。
取得原文、生成DB、トークンはコードリポジトリへ保存しない。

`source_metadata`には元の分類・整理状態・出典説明・URL・資料日・ファイル名を保持する。
空の入力はnull。出典説明は `properties_full` があるとき完全取得済みの値を使う。
`kind=source / claim_kind=source_report / status=candidate`は分類・整理状態で変わらない。
URLは出典参照として返すが内容を検証しない。検索/getは元のmetadataを返す。
DB IDは取得APIの親情報がある場合に保持。過去のsnapshotに無い値は推測で補わず再取得する。

同じ設定・同じ入力の再取り込みはファイルを更新しない。設定内容はrender_settingsに入り、
設定変更は再生成の候補になる。手編集・昇格済み記録は従来のガードで保持する。
新設定導入時は `--dry-run` でrevised/locally_modified/promotedを確認し、通常実行後に再indexする。
[データソース単位の同期](data-source-sync.md)も同じ設定検証とbuild関数を使う。

人工データで検証済み。実Knowledge Databaseの資格情報を使った初回取得は未確認。
