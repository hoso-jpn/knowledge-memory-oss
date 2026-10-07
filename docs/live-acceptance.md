# 実サービスの受け入れ結果を記録する

`knowledge_memory.live_acceptance` は、利用者の実サービスに対する導入確認を1回分の記録として残す。
既存sync・索引・検索・原文取得・SDK probeを順に実行する。
実行対象は明示した設定の許可リストだけ。GitHub/Notionへの書き戻し、候補の昇格、定期実行は行わない。

## 実行前に用意するもの

- インストール済みの `.[mcp]` と、対象サービスを読み取れる実行環境の資格情報。
- 個人用の小範囲を選んだsync設定。[DB同期の設定例](data-source-sync.md)を使い、Blogとはscope・selectionを分ける。
  data_source_idはそのデータソース全体を列挙する。少数ページだけを試す場合は明示page_idを列挙する。
- 実際に原本を読んで期待するID・根拠箇所を固定した質問YAML。
- コードリポジトリ外の、新しい保存先。既存vaultを指定しない。
  既に存在するパスやsymlink、Git作業tree内のパスは開始前に拒否される。

質問の形式は `km evaluate` と共通。次は記法だけの例なので、実資料のIDと質問に置き換える。
自然文の質問は実AIクライアント用、queryはCLIの字面検索用として別々に残す。
複数の資料が必要なら `min_hits` を指定し、少なくとも1件の期待ID付き質問を含める。

```yaml
questions:
  - id: q01
    question: 原本で説明されている仕組みは何か
    query: 原本で確認した検索語
    expect_ids: [src-実ページid]
    evidence: 原本で確認した見出しと根拠箇所
  - id: q02
    query: 原本にないことを確認した語
    expect: unknown
```

## 実行と出力

資格情報は環境変数で渡す。設定、コマンド引数、記録ファイルへトークンを書かない。
設定とそのimport_configへの相対パスは通常のkm-syncと同じ。

```bash
read -rs NOTION_TOKEN && export NOTION_TOKEN
python -m knowledge_memory.live_acceptance \
  --config /outside/knowledge-sync.yaml \
  --questions /outside/knowledge-questions.yaml \
  --workspace /outside/new-acceptance-run \
  --include-candidates --with-mcp
```

`--include-candidates`は初回取り込み資料の明示利用として必須。
`--with-mcp`を省略するとSDK接続はnot_runのまま。実行順は次のとおり。

1. 設定・質問を検証し、新しい保存先のinputsへ解決済み設定と質問を固定する。
2. dry-runが全対象で成功したらsyncする。空の選択結果や部分失敗は失敗として停止する。
3. 再syncで同じ資料の重複がなく、Markdownが変わらないことを確認する。
4. indexを構築し、通常参照のcandidate除外、候補参照、原文get、固定質問の検索結果を記録する。
5. 指定時だけ公式SDKのstdio search/getを実行し、原文ハッシュの一致を確認する。

4の候補制御・期待ID・原文取得・固定質問のいずれかが失敗した場合、SDK試験へは進まない。
停止段階は`index_and_retrieval`のまま、評価結果と取得済みの証拠を保存する。

report.jsonには自動確認の合否、停止段階、各sync結果、質問評価、安定ID・由来・資料日・
編集日時・取得日時、実行版を保存する。JSONのstdoutは状態と保存先だけを表示する。
原本編集日時は`origin.source_edited_at`、資料日は`origin.source_metadata.document_date`から
転記し、取得日時で補完しない。原本にない任意項目はnullのまま記録する。
vault、sync-state内の原本証拠、索引、inputsも同じ保存先に残る。
**これらは私的資料を含むためコミットしない。** Issueへは匿名化した件数・合否・未確認点だけを転記する。

終了コード0はこの一連の自動確認が成功した意味。Issue完了や実LLMによる回答成功ではない。
失敗は終了コード2。開始後の失敗はreport.jsonを残し、後続の索引構築や成功報告を止める。
同じ保存先の再利用を拒否するので、証拠を残したまま別の新しい保存先で再試行する。
中断した同期を調べる場合は既存のkm-sync --resume手順を使う。元資料を削除・上書きして帳尻を合わせない。

## 手動確認は別に記録する

レポートのmanual_checksは全項目not_runで初期化され、自動では合格へ変わらない。
別の受け入れ記録に次の証拠を残す。

| 確認 | 必要な実環境の証拠 |
| --- | --- |
| 本文・表・出典の保持 | 原本とvaultの対応箇所を人が比較。由来・資料日・編集日・取得日も確認 |
| 通常の原本更新 | 自然に発生した追記を明示sync→index→getで確認。疎通用ダミー文を実資料へ追加しない |
| クライアント設定 | クライアント名/版・モデル・導入commitと、AGENTS.md等の読込を示す記録 |
| 自発的な検索と回答 | 自然な質問からモデルがsearch→getを選び、原本の該当箇所に基づいて回答するログ |
| 長い資料と未収録の質問 | 省略表示からgetで末尾を確認すること、時点と不確実性を区別し出典を捏造しないこと |
| 利用保留 | 権限喪失等を古い索引・候補設定で迂回しないこと。実サービスの権限を無断変更しない |

SDK probeのllm_initiatedはfalse。SDKが成功しても上表を代替しない。
具体的なクライアント設定・質問の型・切り分けは[導入と受け入れの手順](knowledge-acceptance.md)を使う。
人工API fixtureと実サービスの結果も分ける。repositoryのテストは前者と実SDK通信だけを扱う。
実サービスの資格情報や実AIクライアントで実行していない受け入れを、完了として報告しない。
