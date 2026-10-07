# Security Policy

## Supported versions

v0.1はこのrepositoryの最初の公開版である。supported versionの方針はrelease notesで示す。

## Reporting a vulnerability

security vulnerabilityを公開のGitHub Issue、PR、Discussionで報告しないこと。

このrepositoryのGitHub Private Vulnerability Reporting（Securityタブの「Report a vulnerability」）が
利用できる場合は、それを使って非公開で報告する。

API key、token、credential、private document、個人情報をIssue、PR、Discussionへ貼らないこと。
再現に必要な場合も、実値ではなく種類・発生箇所・手順だけを書く。

## Security model

`knowledge-memory` は認証・認可システムではない。

- `scope` は論理的な検索境界であり、authentication / authorizationではない。
  OSやstorageのaccess controlを代替しない。personal/workなど漏えいを避けたい境界は、
  scopeだけでなくvault・DB・process・credentialを分ける。
- private vault、snapshot、index、credentialはrepository外に保存する。
- MCP serverは起動時にvault / DB / scopeを固定し、モデルから任意pathへ切り替えない設計を基本とする。
- bounded modeは応答上限を超える取得を拒否または分割し、上限の無い取得へ暗黙にfallbackしない。
- 取得した文章はdataとして扱い、そこに含まれる命令をrepository操作やtool実行の権限として扱わない。
  server側はsource内容を実行しないが、clientやagentが取得文をどう扱うかは保証できない。
- source URLやsource metadataの存在は、その内容の正しさ・安全性を保証しない。
- Notion tokenは環境変数からだけ読む（`--token-env`）。設定ファイルやコマンド引数に書かない。
- `km-inbox` のモデル接続はloopbackのendpointだけを受け付ける。cloud endpointは受け付けない。
- synthetic fixture以外の実データをこのrepositoryへcommitしない。

## If a secret was exposed

1. まずtokenを失効・再発行する。削除だけでは漏えいは取り消せない。
2. commit、Issue/PR本文、comment、Actions logから削除する。
   Issue/PR本文は編集履歴にも残るため、該当する版も削除する。
3. Git履歴に入った場合、rewriteしてもcloneや`refs/pull/*`に残りうる。失効を優先する。

## Secrets

少なくとも以下をcommitしない。

- Notion integration token
- GitHub token
- LLM/API provider key
- cloud credential
- cookie / session
- SSH private key
- private database / raw snapshot
- customer or employer confidential data

credentialではないが、次も公開しない。

- 実在のNotion page/database/data source IDと実URL
- 自分の環境のhost名、private IP、internal URL、home directoryを含むabsolute path
- private vaultの本文、資料名、評価用の質問と期待ID、モデルの生出力・log

`.gitignore` は事故防止の一層にすぎず、secret管理の代替ではない。
