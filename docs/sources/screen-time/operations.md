# 運用

## Collector state

```text
~/Library/Application Support/personal-data-platform/collector.db
```

既存SQLiteに疑似化scope・SHA-256・Raw key・状態・gzip bytesを保存する。DBはmode `0600`、専用directoryは`0700`。
各元ファイルの最新成功版を無期限に残し、未取込分だけ追加保持する。MotherDuckの台帳で成功を確認した後に、
同じscopeの古い成功版を整理する。Biomeから元ファイルが消えても保存済みRawは残す。
device identifierや絶対segment path、tokenは保存しない。

## 初回scanとwatch

```text
pdp screen-time collect --once   全対象をローカル保存してから直接取り込み
pdp screen-time collect --watch  同じ処理を1800秒間隔で反復
```

最新segmentも[取得仕様](acquisition.md#segb-container)に従って検査する。同内容の連続取得はskipし、
`A→B→A`や後着・修正は観測時刻の異なるRawとして処理する。event時刻のwatermarkで捨てない。
read前後のinode・size・mtimeが変われば短く再読込し、構造不整合・CRC不一致のsnapshotは次回へ見送る。

全対象の保存を終えてから、既存Loaderを同じprocess・共通leaseで実行する。成功版の整理はDB確認後だけ行う。
JSONには`devices`、`segments`、`archived`、`skipped`、`retried`、`deferred`と、取込結果の`loaded`、`pending`を出す。
保存だけ成功しても取込成功とはみなさない。不完全snapshotの回も保存済みpendingは取り込めるが、成功heartbeatは更新しない。

`--watch`は通信障害・lease競合・走査失敗をstderrへ記録し、1800秒後に再試行する。
`PDP_COLLECTOR_POLL_SECONDS`の既定値は1800、最小10秒。通常運用は1800秒を維持する。

## Crash recoveryと監視

pendingには元のkeyとgzip bytesを保持する。再起動後も同じbytesで再試行し、commit結果が不明な場合は
再接続後にMotherDuck台帳を確認する。削除処理・イベントkey・補助状態のtransactionは既存の契約を使う。
片方のstreamが走査失敗しても他方を走査する。失敗した回は全体を成功として記録しない。

ローカルreceipt・manifestは従来のkeyと本文でSQLiteへ保存する。未設定streamは空manifestで明示的に休止する。
端末をallowlistから外しても保存済みRawは残す。設定変更はLaunchAgentを再生成・再起動して反映する。

全streamでpendingがなく、取込とローカル監査が成功した回だけ、MacがMotherDuckの
`screen_time_reconciliation` / `screen_time_app_usage_reconciliation` heartbeatを更新する。
Cloud Runの日次Jobは両streamの成功時刻・`scan_completed_at`が48時間以内かを確認し、DB relationとdbtを検証する。
休止streamも成功記録の鮮度を確認する。Cloud RunはMacの成功記録を更新しない。新eventの有無だけで障害を判定しない。

## 設定と診断

```text
pdp screen-time devices
pdp screen-time doctor
```

Keychain service `personal-data-platform`に次の専用項目を登録する。tokenはKeychain Accessなどから設定し、
plist・SQLite・shell履歴・Gitへ保存しない。

| account | 内容 | 一時override |
|---|---|---|
| `screen-time-pseudonym-key-hex` | 32 bytes以上の疑似化secretのhex | `PDP_PSEUDONYM_KEY_HEX` |
| `screen-time-motherduck-token` | 本番MotherDuck writer token | `PDP_SCREEN_TIME_MOTHERDUCK_TOKEN` |

疑似化secretを変更すると過去のdevice / segment keyとの同一性を失う。
`devices`のiPhone keyは`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`、Mac keyは`PDP_SCREEN_TIME_MAC_DEVICE_KEY`に設定する。
接続先は`MOTHERDUCK_DATABASE`。Collectorは汎用`MOTHERDUCK_TOKEN`を使わない。

`doctor`はBiomeのread access、端末設定、SQLite directoryのwrite可否、専用tokenとDB設定を確認する。
実際の接続・Raw decode・DB取込は`collect --once`で確認する。GCS設定・Collector ADCは不要である。

## LaunchAgent

```bash
export MOTHERDUCK_DATABASE="personal_data_platform"
export PDP_SCREEN_TIME_DEVICE_ALLOWLIST="<device_key>[,<device_key>...]"
export PDP_SCREEN_TIME_MAC_DEVICE_KEY="<mac_device_key>"
export PDP_COLLECTOR_POLL_SECONDS="1800"
collector_plist="$HOME/Library/LaunchAgents/com.personal-data-platform.screen-time-collector.plist"
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time launch-agent \
  --output "$collector_plist" \
  --project-root "$(pwd)" \
  --python-executable "$(pwd)/.venv/bin/python"
plutil -lint "$collector_plist"
```

Python executableへ「システム設定 > プライバシーとセキュリティ > フルディスクアクセス」を付与する。
Terminalの権限だけではLaunchAgentから読めない。Python更新・仮想環境再作成後は再確認する。
plistはtokenを含まず、生成時のmodeは`0600`、log directoryは`0700`。

```bash
launchctl bootstrap "gui/$(id -u)" "$collector_plist"
launchctl print "gui/$(id -u)/com.personal-data-platform.screen-time-collector"
tail -F "$HOME/Library/Logs/personal-data-platform/screen-time-collector.stdout.log" \
  "$HOME/Library/Logs/personal-data-platform/screen-time-collector.stderr.log"
```

設定変更・token更新・Rebuild前は先に停止する。

```bash
launchctl bootout "gui/$(id -u)" "$collector_plist"
```

## LoaderとRebuild

Collectorが通常の取り込みを行う。手動の`pdp loader --source screen_time --all-streams`と
`pdp rebuild --source screen_time --all-streams`も同じローカルRawを読む。
Rebuild中はCollectorを停止し、inventory取得から再生完了までRawを固定する。
別の空scratch DB、`--allow-partial-history`、dbt検証などは[Platform運用](../../platform/operations.md#rebuild)に従う。

イベント・補助状態・台帳は同じ時点でバックアップ・復元する。保存するのは各ファイルの最新版であり、
更新前Rawによる再解析とRawだけからの全履歴復元は保証しない。通常の切替ではMotherDuckの既存履歴を保持する。

## GCSからの一度限りの移行

旧Collectorと両Schedulerを止め、実行中Job・leaseがないことを確認し、DBのイベント・補助状態・集計を比較基準として記録する。
既存SQLiteとplistを一時バックアップし、本番writer tokenを専用Keychain項目へ登録する。

```bash
.venv/bin/python scripts/migrate_screen_time_raw.py \
  --project health-data-pipeline-503813 \
  --bucket health-data-pipeline-503813-pdp-raw-west \
  --bucket health-data-pipeline-503813-pdp-raw \
  --state-db "$HOME/Library/Application Support/personal-data-platform/collector.db" \
  --manifest var/screen-time-cutover/gcs-deletion-manifest.json
```

実行元のgcloud userには対象GCSのlist/read権限が必要である。現行bucketを最初に指定する。
各objectをgeneration固定で読み、gzipとSHA-256を検証する。未取込Rawは既存Loaderで確定してから最新版を移す。
既存pendingは保護し、再実行しても新しいローカルRawを巻き戻さない。payloadの中間ファイルは作らず、
manifestにはbucket・疑似化key・generation・hash・圧縮byte数だけを記録する。

新CollectorとCloud Runを反映し、30分周期を2回、履歴保持・最新event・pending解消・日次Job・dbtを確認する。
すべて成功した後だけ、manifestに記録したgenerationを指定して両bucketのScreen Time Rawとcontrolを削除する。
generationが変わっている場合や検証失敗時は削除しない。Fitbitやbucket自体は削除対象に含めない。
最後に両prefixの空状態とScheduler再開を確認し、移行用の一時データと旧専用Collector ADCを整理する。

2026-10-08の切替・検証・撤去結果は[切替記録](local-raw-cutover.md)を参照する。
