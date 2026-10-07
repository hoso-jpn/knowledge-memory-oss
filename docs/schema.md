# 記録形式 v1

YAML frontmatterとMarkdown本文。8必須項目は取り込み側で生成し、人は内容・出典・適用条件を確認する。

| 項目 | 意味 |
| --- | --- |
| id | 安定ID。英小文字開始、英小文字・数字・`.`・`_`・`-`、128文字以内 |
| title | 空でない題名 |
| kind | note / source / idea / log / moc / decision / project / procedure |
| status | candidate / active / superseded / retracted |
| scope | 個人研究などの境界。IDと同じ形式 |
| claim_kind | user_statement / observation / source_report / hypothesis / interpretation |
| updated | ISO日付またはタイムゾーン付き日時。編集時点 |
| sources | `ref`と`locator`を持つリスト。activeでは1件以上必須 |

任意項目：`domain`、`aliases`、`related_ids`、`supersedes`は文字列リスト。
`event_at`は出来事の時点、`verified_at`は根拠を確認した時点。未確認なら省略またはnull。
値を埋めること自体を確認済みの証拠にはしない。確認の方法と結果は本文へ記録する。

## 取り込み元 `origin`（任意）

取り込みで作った記録だけが持つ。原本そのものではなく、いつ・どの原本の・どの行を読んだかを残す。

必須：

| 項目 | 意味 |
| --- | --- |
| kind | 取り込み経路。Notion APIは `notion_api`、CSVは `notion_csv_export` |
| record_id | 原本側の安定した識別子。NotionはページID、CSVはレコードID |
| content_sha256 | 読み取った内容のSHA-256。改訂の検出に使う |
| retrieved_at | 原本を読んだ時点。タイムゾーン付き |

任意：

| 項目 | 意味 |
| --- | --- |
| source_url | 原本のURL。Notionページなど |
| source_edited_at | 原本側の最終編集時点。Notionの `last_edited_time` |
| body_from | 本文をどこから採ったか。`page_body` または `property:<名前>` |
| body_rule | その採用元を選んだ理由。再実行時に同じ選択になることを確かめられる |
| render_settings | 変換の版と設定。これが変われば内容が同じでも作り直す |
| render_sha256 | 生成したノート自身のダイジェスト。手編集の検出にも使う |
| source_file | 原本のファイル名のみ。パスは持たない（CSV経路） |
| source_file_sha256 | 原本ファイル全体のSHA-256（CSV経路） |
| published | 原本のPublishedの値をそのまま |
| status_label | 原本のStatusの値をそのまま |

`origin`を持つ記録は`claim_kind: source_report`に限る。原本が何と書いていたかの報告であり、
その内容が正しいという主張ではない。

`retrieved_at`は取り込み時点、`source_edited_at`は原本側の編集時点、`event_at`は記事の日付、
`updated`は記録の編集時点で、すべて別物。
`published`が`Yes`でも`status`は`candidate`のままで、`verified_at`は付けない。
公開済みであることと、現行の原本と突き合わせて確認したことは別の事実である。

`active`は通常の検索で利用する状態であり、科学的に正しいというラベルではない。
仮説もactiveになり得るが、claim_kindと限界をそのまま返し、事実へ格上げしない。

`related_ids`と`supersedes`は同じvault・scopeに存在する別IDだけを指定する。
`supersedes`で指す旧記録はsuperseded状態にする。新記録と旧記録を同じ変更で更新する。
出典refの外部存在確認やIDリンクとしての解決は現段階で行わない。sourceのrefは任意パス取得の命令ではない。

```yaml
id: decision-example-001
title: 検討した選択と理由
kind: decision
status: candidate
scope: personal-research
claim_kind: user_statement
updated: '2026-09-21'
sources:
  - ref: conversation:local-record-001
    locator: 判断理由を述べた箇所
```

本文には「内容」「理由または根拠」「適用条件」「限界・例外」を残す。
出典中の発言を確認した場合はsource_report、独立した実測はobservationなど、何を記録したかで分ける。
AI生成文は抽出候補としてcandidateに置き、承認と根拠の確認まで原本や現行方針を上書きしない。

`content_sha256` は**原データの同一性**、`render_sha256` は**出力の同一性**を表す。
後者は `render_settings`（変換の版と設定）も含むため、原データが同じでも取り込み設定や
変換実装が変われば別物として作り直す。`retrieved_at`・`updated`・`source_file_sha256` は
「その読み取り」に属する値なので `render_sha256` の計算からは除く。同じ記録を読み直しただけで
全件が書き換わらないようにするため。

`render_sha256` はノート自身に対するダイジェストなので、取り込み後に人が手で編集すると一致しなくなる。
取り込みはこれを `locally_modified` として報告し、上書きしない。`active` などへ昇格済みの記録は
`promoted` として報告し、上書きしない。既存の記録と `scope` が異なる取り込みは実行前に中止する。

取り込みの層は三つに分ける。Notionが正本、取得結果を保存したスナップショットが中間、
検索用のMarkdownとSQLiteが派生物。`origin`が指すのはスナップショットを読んだ事実であり、
その時点以降の正本の変更は反映しない。

## 明示的なNotion学習資料設定（mapping/1）

`origin`の追加任意項目は `database_id` / `data_source_id`（原本所属の識別子）、
`source_created_at`（原本作成日時）、`source_metadata`。作成日時と編集日時・取得日時を分ける。
識別子はpage.parentとsnapshot manifestを用い、両方にある値が食い違えば取り込みを止める。
突き合わせは両方に値がある場合だけ成立する。片方にしかない場合はその値をそのまま記録し、
所属を照合したことにはしない。記録された識別子は「この取得でそう申告された所属」であり、
Notion側で現在もその所属であることの保証ではない。

`source_metadata`を持つ場合は次の6キーをすべて必要とし、未知キーを拒否する。
空欄はnullで保持し、別のプロパティで補わない。従来originの文字列項目の検証は変更しない。

| キー | 型と意味 |
| --- | --- |
| source_type | string/null。原本の「種別」。kind/claim_kindを変えない |
| editorial_status | string/null。原本の「整理状態」。status/verified_atを変えない |
| source_description | string/null。完全取得した「出典」の説明 |
| reference_url | string/null。原本記載の出典URL。sourcesにも未検証参照として追加 |
| document_date | nullまたはstart必須・end/time_zone任意のobject。ISO資料日/版の日付。event_at/verified_atに転記しない |
| original_filename | string/null。「元ファイル」rich_text。filesプロパティではなく原本記載値 |

日時はISO日付またはtimezone付き日時。domainは指定した「分野」だけから取得する。
`render_settings`には明示設定の版と全内容を保存する。

## Notion変換 notion/3の変更判定

任意の `origin.semantic_sha256` は64桁の小文字SHA-256。生recordから既知の読み取りごとの値だけを
正規化した同一性を示す。正規化するのは次の2つだけで、ほかのキーは名前だけを見て消さない。

- Notionが持つfileオブジェクトの署名付きURLの署名クエリと `expiry_time`
- 応答エンベロープの `request_id`（問い合わせ照合用に毎回変わる。`object` キーを持つ
  オブジェクトのものだけを対象とし、利用者が `request_id` という名前で作ったプロパティは残す）

`content_sha256`の生データ同一性という意味は維持する。
semantic_sha256のある記録だけrender_sha256からcontent_sha256を除くため、署名URLと
request_idだけの変化はrevisedにならない。これを入れる前は、無変更のページが毎回revisedになった。unchangedのノートは過去の取得時点とraw hashを保つ。
最新の取得証拠は生snapshot/同期stateに残る。詳しくは[変換仕様](notion-conversion.md)。

`km context`は、取り込み記録に取得時点・原本編集時点・資料日を表示する。
それぞれの時点を内容の検証日時と混同しない。全文と詳細metadataはgetで参照する。
