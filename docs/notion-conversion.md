# Notion変換 notion/3

表はHTMLのtable/tr/td/thで出す。Notionのtable_widthを検証し、列ヘッダーは最初の行の
th scope=col、行ヘッダーは最初の列のth scope=rowとする。双方を持つ左上はcol。
ヘッダーなしは全てtdとし、見出しを捏造しない。空セル・順序・日本語・パイプを保ち、
セル内改行をbr、リンクをaへ変換する。HTML特殊文字はエスケープする。幅の不一致は取得結果の
問題として停止し、足りないセルを推測で補わない。Markdownだけを扱う閲覧側でも行列タグは残る。

file/image/video/audio/pdfのNotion-hostedファイルは、名前（APIにあれば）、caption、block ID、
Notion原本URL＋ブロックアンカーを控えとして出す。短期URLは生snapshotだけに残す。
ファイル名がAPIにない場合は種類を表示し、URLから推定しない。外部URLは通常リンクとして保持する。
バイナリは取得しない。添付MDの内容を本文へ結合しない。table_of_contents/breadcrumbはnavigationとして
省き、conversion_reportsへ記録。未知ブロックは未対応表示＋レポートに出し、既知rich_text/childrenは保持する。
未知の構造化payload全体は生snapshotで確認する。未取得のsynced_blockも明示する。

conversion_reportsは**採用した本文**についてのみ返す。採用元を決めるためページ本文は必ず変換するが、
Contentプロパティを採用した場合は、使わなかったページ本文のレポートを返さない。
プロパティ本文にブロックの概念はないため、その場合のレポートは空になる。

## 変換が失敗したときに残るもの

幅の不一致のような取得結果の問題は例外にする。黙って飛ばさず、推測でも補わない。
そのときの書き込みは経路ごとに違う。

- `import-notion`：全記録を組み立ててから書き出すため、1件でも失敗すると**何も書かない**。
  既存ノートはバイト単位で不変で、一時ファイルも残らない。再実行すれば最初からやり直す。
- `km-sync`：source単位で捕捉する。失敗したページだけledgerが`needs_review`になり、
  そのページはsearch/get/contextから除外される。同じ実行の他のページは保存され、
  runは未完了として報告される。原本ノートは消さない。

どちらも部分的に壊れた本文を保存しない。失敗した記録を「本文なし」として取り込むこともしない。

## ハッシュの責務と再生成

- content_sha256：取得した生recordのcanonical JSONのハッシュ。従来の意味を維持。
- semantic_sha256：同じrecordの既知file objectからexpiry_timeと署名用query値だけを除いたハッシュ。
- render_sha256：変換設定と出力。semantic_sha256のあるノートだけ、content_sha256を計算から除く。
  過去のノートに対するrender_digest検証は従来通り。本文・表・通常URL・ファイル名・block ID・
  URLのhost/path・署名以外のquery・原本編集時点などは無視しない。

生snapshotと取得証拠は正規化せず保存する。署名だけの更新でunchangedとなったノートは
既存の取得日時/raw hashを維持する。最新の取得証拠はsnapshot/sync側で確認する。
URLのpath/ファイルIDが変われば差し替えとして検出する。ただし同じ識別情報のバイナリだけが
変化してAPIの編集時点も不変な場合、ダウンロードしないため検出できない。
queryはAWSのX-Amz署名キーおよびSignature/Expires/AWSAccessKeyIdだけを除く。
未知の署名方式は推測で消さず、差分扱いになる場合がある。

notion/2のノートは初回にrevisedとなる。既存の同じ取り込み設定で `import-notion --dry-run` を確認し、
通常実行→indexを行う。手編集/昇格済みノートは保護され、手動レビューが必要。
データソース単位のsyncも同じbuild/変更判定を通す。本文とContentプロパティのブログ回帰テストも実行する。

## 正規化の対象とするfile object

File objectの`type`で扱いを分ける。正規化は`type: file`（Notion-hosted）だけが対象。

| type | payload | 扱い |
| --- | --- | --- |
| `file` | `url`（期限付き）, `expiry_time` | 署名queryと`expiry_time`をsemantic_sha256から除く |
| `file_upload` | `id` | 正規化しない。`id`が変われば差し替えとして検出する |
| `external` | `url`（永続） | 正規化しない。URLが変われば実質変更として検出する |

`type`配下にurlがあれば一律に除く、という実装にはしない。`external`の永続URLの変更を
見逃すため。`type`が`file`から`file_upload`や`external`へ変わった場合も差し替えとして検出する。

仕様確認: [Block](https://developers.notion.com/reference/block)、
[File](https://developers.notion.com/reference/file-object)（2026-09-26）。
人工fixtureのみで検証している。実際のNotion画面と対比した受け入れは利用者側の確認事項である。

## API取得の完全性

既定のNotion-Versionは2025-09-03を維持する。`fetch-notion --database` は
Databaseのdata_sourcesが1件の場合だけそのDSを取得する。0件・複数件では停止し、
`--data-source`で明示的に選ぶ。複数DSを自動的にまとめない。
旧`/databases/{id}/query`は明示した`--notion-version 2022-06-28 --database`に限定する。
対応する版はこの2つで、未知の版と旧版での`--data-source`は通信前に拒否する。

初回取得は最大3要求、再試行の合計待機は最大30秒。Retry-Afterが残り待機予算を
超えると期限より前に再送せず、`retry_later`と秒数・再開可能時刻を返す。
不正なヘッダーは停止する。上限は再試行待機の合計であり、HTTPの処理時間全体ではない。
差分同期の既存の最大3要求・1待機30秒までの制御も維持する。
エラー応答の本文やURLは初回取得の診断へ転載しない。

全ページのrequest_statusを検査し、incompleteや未知の状態は失敗とする。
`has_more: false`だけを完全取得の証拠にしない。初回DS取得も10,000件以上を拒否する。
成功済みsnapshotは不完全取得で置換しない。同期では既知の資料を利用保留にし、
不完全列挙から所属除外・削除・正常復帰を判定しない。資料本文は保存する。
大量データのwindow分割は未実装。実Notionの大量取得・権限変更は検証していない。

一次資料（2026-10-02確認）：[要求制限](https://developers.notion.com/reference/request-limits)、
[大量DSの完全性](https://developers.notion.com/guides/data-apis/query-large-data-sources)、
[2025-09-03移行](https://developers.notion.com/guides/get-started/upgrade-guide-2025-09-03)。
