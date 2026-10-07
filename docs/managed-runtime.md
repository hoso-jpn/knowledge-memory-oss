# 同期・索引・MCPの管理運用

`knowledge_memory.runtime` は、同期→索引の更新とMCPの読み取りを1つのロックで直列化し、
状態・再試行・復元後の照合を明示的なコマンドとして提供する。常駐やtimerは作らない。
定期実行する場合は利用者がscheduler側で設定する。
Linux/POSIXのローカルファイルシステムを対象とし、NFS等のロック保証は未検証。
同梱のtestは人工データと一時ディレクトリで行っている。実配備での導入・復旧は利用者が別に受け入れる。

## 配備前の照合

[プロファイル検査](profile-boundaries.md)を通し、Blog/個人/業務の入力・vault/index/state・プロセスを別に固定する。
AI接続には[応答上限付きのboundedモード](mcp.md)を使う。
コード・環境の例は新規の人工ディレクトリで試し、実IDと秘密はリポジトリ外へ置く。

```bash
python -m knowledge_memory.runtime manifest --config /outside/config/personal.yaml \
  --commit FULL_40_CHARACTER_SHA
```

manifestはパッケージ内全Pythonファイルのhash、合成manifest hash、Python/SQLite/依存版、
import mappingを含む解決後設定hashを出す。commitは利用者が入力したラベルで、照合済みとはしない。
採用commitのファイルmanifestと実際の配備先を比較し、差分と欠落を残す。
ディスク上のファイル一致は、既に起動したプロセスがそのコードを読んだ証明ではない。
配備の棚卸しでは、接続するクライアント・ブリッジ・モデルの版、起動引数、認証経路、件数、間隔も別に記録する。
資格情報・実パス・本文・署名URLをGitHubへ転載しない。hashも匿名化の保証ではない。

## 共通ロックを使う最小構成

新規の試験用環境でsync設定と環境変数を用意する。Notionトークンはプロセス環境へ渡し、
コマンド引数や設定例に値を書かない。

```bash
python -m knowledge_memory.runtime sync-index \
  --config /outside/config/personal.yaml --vault /outside/km/personal/vault \
  --database /outside/km/personal/index.sqlite3 --state /outside/km/personal/state
km-mcp --vault /outside/km/personal/vault --db /outside/km/personal/index.sqlite3 \
  --scope personal --include-candidates --bounded --serialized \
  --response-max-chars 12000 --response-max-bytes 24000
```

sync-indexは同期が完全成功した場合だけ索引を作り直す。取得失敗では元ノートと旧索引を保持し、
ledgerの利用保留を維持する。部分更新で旧索引がstaleになった場合もMCPは拒否する。
保護判断locally_modified/promoted等の既存sync仕様は変更しない。statusの内訳を確認する。
列挙から消えたページの確認失敗も失敗として扱い、retry_laterなら自動再送を保留する。
確認済みのarchived/movedは保留情報を維持して索引化できる。入力設定とimport mappingは
vault/index/stateの外に置く。管理先同士の包含・共有も、通信や書き込みの前に拒否する。

両プロセスはvault内の同じ`.km-runtime.lock`を要求全体で排他取得する。
writerと同じvaultを読み取り専用でbind mountするreaderでは、このinodeを作成できない。
`.km-runtime.lock`を**読み取り専用にする前のvaultへ用意**しておく。dotfileは索引対象外なので
索引の鮮度判定には影響しない。writer/readerの実行ユーザーがそれぞれ開ける所有者・権限を確認し、
readerがvaultへ書けるように権限を緩めない。readerは読み取り専用の記述子で同じinodeをflockし、
writerの排他取得と衝突する。inodeが無い読み取り専用vaultでは、排他なしで続行せず明示的に失敗する。
重複起動・同期中のsearch/get/contextは待ち続けずbusyを返す。クライアントが自動で連続再試行しないよう設定する。
OSがプロセス終了時にロックを解放する。ロックファイルは残し、動作中にunlinkしない。
既存`.km-sync.lock`は引き続き使われ、異常終了時に残ればwriter不在を確認して既存手順で復旧する。

**copytree等で別の公開世代へコピーしたlockは別inodeであり、live writerとは排他されない。**
その世代のledgerもコピー時点で止まり、live側の保留更新は伝わらない。読み取り専用でlockを
取得できることと、現行writerとの共有・失効情報の反映は別の条件である。最小構成では同じ
vault/ledger/lockの実体をwriterとreaderで共有する。索引内に記録されたvaultの絶対パスも含め、
両側から同じ実体へ正しく解決できるようにする。
索引は原子的に置換されるため、コンテナには索引の親ディレクトリをmountし、更新前の単一DB
ファイルのinodeへ固定しない。別世代を配信する場合の失効伝達・再接続管理はこのlock修正では提供しない。

これは協調するコマンドのロック。手動編集、旧km-sync、直接のkm index、serializedを付けないMCPは
自動では参加しない。管理運用へ移す前に全writerとreaderを棚卸しし、直接操作は停止時間帯に行う。
コード更新も処理停止・manifest照合・再起動を一組とし、稼働中のファイル置換で適用済みにしない。

## 状態と再試行

```bash
python -m knowledge_memory.runtime status --state /outside/km/personal/state
```

state/runtime-status.jsonへ原子的に保存する。最終試行、同期成功、索引までの成功、最後の完全DS列挙、
索引hash/件数、所要時間、source/列挙の結果別件数、連続失敗、次回可能時刻、処理段階を区別する。
ページ単位選択に完全DS列挙時刻は付けない。logical_api_callsはfetch関数の呼び出し数で、
内部の再送を含むHTTP試行回数ではない。実APIコストをこの数だけで断定しない。

失敗後の既定backoffは300秒（`sync-index --backoff-seconds`で変更可）。これは推奨同期間隔ではない。
定期実行を設定する場合は、測定した要求数・所要時間・変更率に基づき間隔を決め、短い間隔を無条件の既定値にしない。
再起動してもstatusのbackoffを読む。phaseがsync/indexのままなら前回中断として記録する。
最後の成功時刻と古いindex hashは履歴として残り、今回の成功を意味しない。

既存syncがretry_laterを返すと、元Retry-Afterの期限をこの層で観測できないため自動再送を保留する。
期限を0や300秒で補わない。運用者が元の制限情報を確認して将来の時刻を指定した場合だけ、次を使う。

```bash
python -m knowledge_memory.runtime release-retry --vault /outside/km/personal/vault \
  --state /outside/km/personal/state --not-before '2099-01-01T00:00:00+00:00'
```

日時は架空例。実際の確認済み期限より前に設定しない。statusには運用者指定でありAPIヘッダーの
自動検証ではないと記録する。指定時刻前の起動は要求を送らない。statusを消して制限を迂回しない。
通信断・列挙不完全・権限不明・索引失敗をreadyと扱わず、本文やエラー応答の転載なしで追跡する。

## 導入・更新・復旧の受け入れ

1. 現行配備を読み取りで棚卸しし、適用差分を確認する。新規人工環境で設定とロックを試す。
2. 許可された停止時間帯で定期実行とreaderを止め、ノート/ledger/registry/stateとコードmanifestを保存する。
3. 選択範囲を拡大せず、同期→索引→MCPの順で新構成を確認する。接続するクライアント・ブリッジの版と設定を固定し、
   boundedのツール一覧、busy、省略、続き取得、取得時点表示を少数例で確認する。
4. 失敗時は新構成を接続しない。元ノートを上書きせず、現行の保留情報を保持して切り分ける。
   境界を検証していない旧経路へ戻すことや、止めた旧い定期実行の再開を戻し方にしない。
   対象Notion経路を無効化して切り分け、境界を検証した分離構成だけを再接続する。
5. 復旧は既存のoperations手順でノート・ledger・registryを組として戻し、別領域で索引を再構築する。
   runtime-status/checkpointも元のprofileへの対応を保つ。バックアップ時点の古いcurrent情報で
   現在の失効を消さず、現行allowlist・保留を突き合わせるまでAI接続を再開しない。

```bash
python -m knowledge_memory.runtime reconcile \
  --config /outside/config/personal.yaml --vault /outside/km/restored/vault \
  --current-vault /outside/km/current-reference/vault
```

検索はledgerにある`current`以外のIDを保留する。ledgerに無いIDは遮断されないため、
古い世代の`current`や欠落したledgerを戻して索引を作り直すと、現在の失効資料も再び返り得る。
同じallowlistのまま権限喪失・削除が起きることもあり、設定だけでは現行の保留を判定できない。

`--current-vault`には、**現行allowlistで同期した後の保留を保持する別の整合したsnapshot**を渡す。
古いbackupの複製で代用しない。復元で上書きする前に保存し、取得時点・config・profile対応を
非公開の運用記録に残す。復元対象と同じパス、包含関係、metadataのhardlink共有は拒否する。
読み取り専用の照合はwriterをロックしないため、両snapshotのwriterを停止し、照合から
再接続判断まで設定・ノート・ledger・registryを変更しない。

reconcileは通信も書き込みもせず、両vaultのノート・ledger・registryと現行allowlistを読み、
次を報告する。対象は**全ノートがledgerで管理される同期用vault**に限る。

- `would_be_served_but_unselected`: 復元側はcurrentだが、現行のページ/GitHub選択や当該DSに含まれない
- `would_be_served_but_held_now`: 現行側が保留・未登録、または現在のDS所属が選択範囲外
- `held_in_ledger`: 復元側で既に保留されている資料
- `owned_by_another_selection`: いずれかのvaultで別selection/scopeに所属する資料
- `would_restore_different_note_status`: 復元によりcandidate/active/retracted等の利用状態が変わる資料
- registryのprofile対応と、現行registryが現行allowlistに一致するか

未登録ノート、ノートID/原本/所属の不一致、currentノートの欠落、不正・欠落metadataは拒否する。
不一致・確認不能は終了コード2。参照が揃っているときだけ`reconciled: true` / 終了コード0。
**参照snapshotの現在性、本文の同一性、現在の外部権限を保証するものではなく、MCPの起動を
自動で制限する機能でもない。** exit 0だけでreaderを再接続しない。既存の承認・取得時点・
境界検査・索引再構築も確認する。

現行の保留を保持した参照が無い場合は合格にできない。許可された隔離領域で現行allowlistの
再取得・所属確認を行い、保留とノートの対応を復旧してから再照合する。ledgerを空にしたり
全件currentへ書き換えたりして通さない。reconcileは保留の反映やノートの更新を自動では行わない。
`km-ops recovery`は現行vaultを往復させる整合確認であり、古いbackupの失効は検出しない。

オフラインで取得済み資料を参照する場合は、originの取得時点/原本編集時点とstatusの最終成功を確認する。
readyは同期時点の取得成功であって、現在のNotion内容や主張の正しさの保証ではない。
このmoduleは外部公開の経路やHTTPブリッジを提供しない。実機導入と復旧の全経路は利用者が別途受け入れる。

## 同期証跡の容量

変更なしの同期でも、生応答のrequest_id等が変わると証跡は増える。
保守時間に[保持計画と明示的な削除](sync-evidence-retention.md)で確認する。
`km-retention plan`は読み取りのみ、`apply`は確認した計画のhashを必要とする。
現行・保持snapshotのraw参照、利用保留、列挙checkpointを保護し、復旧と削除を同時実行しない。
同期間隔を変えるだけでは保持容量の上限を保証しない。保護対象だけで予算を超える場合も報告する。
