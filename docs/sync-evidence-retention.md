# 同期証跡の保持と容量管理

同期は取得した生応答を証跡としてstateへ保存し、自動では削除しない（[sync](sync.md)）。
`km-retention plan` は証跡を読み取り、`apply` は確認した計画の削除候補だけを処理する。
自動起動・定期実行への追加は行わない。挙動は人工の証跡によるtestで確認している。

## 対象と保持するもの

対象は明示した1 profileのstate直下にある `evidence/*.json` と `enumerations/*.json` だけ。
ノート、ledger、registry、checkpoint、索引、backup、公開世代、その他のstateファイルは削除しない。
生応答をsemantic hashへ置き換えず、保存済みのrawとファイル名のhashを照合する。

以下のいずれかに当てはまるファイルは保護する。

| 保護理由 | 内容 |
| --- | --- |
| note_or_ledger_reference | 現行ノートのoriginとledgerの最新取得hash。unchangedでは両者が異なるため両方残す |
| held_source_history | current以外の利用状態を持つ資料の証跡履歴すべて |
| checkpoint | 完了した最新checkpointが参照する列挙証跡 |
| latest_per_source | sourceごと／data sourceごとの最新Nファイル。Nの既定は2、最小1 |
| within_retention_period | mtimeが指定した保持日数内にあるファイル |
| explicit_pin | retention-pins.jsonでhashとともに保持指定したファイル |
| unknown_source / unknown_data_source | 現行・指定snapshotと対応が付かない資料／data sourceの証跡 |

`--keep-days` は**必須の最低保持期間**。7日という下の例は実配備の推奨値ではない。
mtimeを用いるので、コピー時の時刻保存方法やホスト時刻のずれを確認する。最新N件もmtime順で判断する。
参照中・保留中・保持指定の証跡は期間を過ぎても残る。`--budget-bytes` は予算超過の観測用で、
予算に収めるために保護対象を削除することはない。未変更でもrequest_idが毎回変わればrawは増える。
Notionの添付・ファイルブロックは**署名付きURLと失効時刻**を含み、取得間で値が変わることがある。
その場合は本文が同じでもrawは別になるため、request_idと同じく蓄積の原因になる。
本文が変わっていない同一sourceの連続する証跡でも、`request_id`・署名付きファイルURL・その失効時刻
だけが異なることがある。保持日数を決める前に、自分の証跡で差の内訳を確認する。
稼働中の増加は別時点のplanを比較して測り、1回の容量測定だけで増加速度を断定しない。

## 前提

- Linux/POSIXのローカルFS。全writer、定期実行、管理操作を止めた保守時間に行う。
- vault・state・設定は対応する1 profileの独立した保存先。ledger/registryが完全で、
  checkpointが現行config・vault・selectionに対応して完了していること。中断・不一致は拒否する。
- `.km-runtime.lock` が既にあること。`runtime sync-index` が用意する。planのために勝手に
  作成しない。旧配備の初期化はwriter不在を確認した移行手順で行う。稼働中にlockをunlinkしない。
- plan/applyは既存runtime lockを排他取得する。applyは旧km-sync用のlockも取得する。
  plan中の旧writer起動も禁止する。非協調の手編集、特権プロセスによるディレクトリ差替え、
  NFSのロック保証は対象外。MCPツールとして公開しない。
- 保持するbackupを先に棚卸しする。`km-ops backup`はノートと制御ファイルを自己完結して保存するが、
  raw証跡を含まない。rawまで保存したいbackupは下のsnapshot指定またはpinが必要。

## 削除計画を確認する

以下のパスと日数は人工例。planの保存先はvault/stateの外に置く。

```bash
umask 077
km-retention plan --config /srv/example-config/sync.yaml \
  --vault /srv/example-vault --state /srv/example-state \
  --keep-days 7 --keep-latest 2 --budget-bytes 1073741824 \
  --protect-vault /srv/example-backup/restored-vault > /srv/example-reports/retention-plan.json
```

`--protect-vault` は繰り返し指定できる。同じprofileの停止済み・独立したsnapshotを渡す。
そのノートとledgerが参照するrawも保護する。ZIPは `km-ops restore` で別領域へ復元してから指定する。
共有inodeや入れ子、symlinkの制御ファイルは使わない。比較元が最新という保証を与える機能ではない。

個別の監査用証跡はstateの `retention-pins.json` に保持指定できる。
形式は `{"version": 1, "files": {"evidence/<source hash>-<file hash>.json": "<file hash>"}}`。
実際のplanから得た相対パスとhashだけを設定する。不存在、不一致、重複JSONキー、対象外パスは拒否する。
保持指定の変更後は計画を作り直す。

planは内容・サイズ・mtime・ledger/registryを変更しない。atimeはFSの設定に従う。
ファイルごとの相対パス、byte数、mtime、hash、保護理由、削除候補と集計、空き容量を返す。
本文は返さないが、**実パスやhashも含む非公開の運用証跡**として扱い、GitHubにアップロードしない。
最大128MiBのJSONを1ファイルずつ読む。全rawを同時にメモリへ載せないが、全件のhash検査によるI/Oは発生する。

現在のノート・ledgerのrawがstateに見つからない場合は、すべての削除を拒否する。
初回import等のrawが別snapshotにあるなら出所を確認し、必要な証跡を正しい形式で保全してから再検討する。
pinを追加するだけで不足したrawを存在することにはできない。検査を迂回して削除しない。

## 確認した計画を実行する

削除候補・保護理由・保持backupを確認した後、表示された `plan.plan_sha256` を明示する。
計画は24時間以内に作ったものを使う。以下の `PLAN_SHA256` は実際の64桁値へ置き換える。

```bash
km-retention apply --plan /srv/example-reports/retention-plan.json \
  --expect-sha256 PLAN_SHA256 > /srv/example-reports/retention-result.json
```

同じpolicy・基準時刻で全件を再検査し、計画全体が一致してから削除する。
その間に同期、設定・pin・参照snapshot・候補ファイルの変更があれば計画を作り直す。
`apply` は確認済みファイルをunlinkするため、取消には事前の独立backupが必要。
削除は複数ファイルをまとめた原子的処理ではない。途中のI/O失敗は `completed: false`、
削除済み一覧、停止箇所を返してexit 2。電源断では結果出力が残らない場合もある。
再開時は現状を点検して新しいplanを作る。古い計画の再実行や一括rmで続きを処理しない。

保持日数は証跡全体の期間と比べる。全証跡が指定日数より新しい場合、planの削除候補は0件になる。
短い間隔で大量に蓄積した直後は、保持日数を縮めるか、期間が過ぎるまで待つかの選択になる。
同期間隔を延ばしても、新しいrawが生じる同期を続ける限り証跡は増える。
旧profileのwriterをすべて停止した場合に、その旧profileの新規蓄積が止まる。
保持期間を過ぎたファイルも、参照・利用保留・pin・指定snapshot等の保護があれば残る。
後日実行する際は、その時点の保護対象を反映した新しいplanで候補量を確認する。
特定の日付になれば削除できると保証せず、以前のplanを再利用しない。

成功後も予算超過が残り得る。その場合は保持対象の内訳・同期頻度・保存先容量を見直す。
実配備での保持日数、監査・復旧要件、対象一覧の確認と実行は利用者が別途行い、人工試験で代替しない。
別の公開世代へコピーした索引・vaultの参照停止と失効管理はこのコマンドの対象外である
（[管理運用](managed-runtime.md)）。
