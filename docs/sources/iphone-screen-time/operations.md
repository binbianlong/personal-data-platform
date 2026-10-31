# iPhone Screen Timeの運用

## Collector state

```text
~/Library/Application Support/personal-data-platform/iphone_screen_time/collector.db
```

DBはmode `0600`、専用directoryは`0700`。Rawと取込状態の保持範囲は[データモデル](data-model.md#observation-semantics)を参照する。

## 初回scanとwatch

```bash
pdp iphone-screen-time collect --once
pdp iphone-screen-time collect --watch
```

このsourceの全対象を[取得仕様](acquisition.md#segb-container)に従って保存してからLoaderを実行する。
保存だけ成功しても取込成功とはみなさず、JSONの`loaded`と`pending`を確認する。
不完全snapshotの回も保存済みpendingは取り込めるが、成功heartbeatは更新しない。
`--watch`は通信障害・lease競合・走査失敗をstderrへ記録し、次周期に再試行する。
他方のCollectorは別process・設定・SQLiteを使う。共通`loader` leaseによる書込競合は延期して再試行する。

## Crash recoveryと監視

再起動後もpendingの同じkey・bytesで再試行し、commit結果が不明な場合は再接続後にMotherDuck台帳を確認する。
端末を設定から外しても保存済みRawは残す。設定変更はLaunchAgentを再生成・再起動して反映する。

このsourceでpendingがなく、取込とローカル監査が成功した回だけ、MacがMotherDuckの
`iphone_screen_time_reconciliation` heartbeatを更新する。他方のsourceのheartbeatは更新しない。
Cloud Runの日次Jobはこの成功時刻・`scan_completed_at`が48時間以内かを確認し、DB relationとdbtを検証する。
Collectorを停止したsourceも成功記録の鮮度を確認する。Cloud RunはMacの成功記録を更新しない。新eventの有無だけで障害を判定しない。

## 設定と診断

```bash
pdp iphone-screen-time devices
pdp iphone-screen-time doctor
```

Keychain service `personal-data-platform`に次の専用項目を登録する。tokenはKeychain Accessなどから設定し、
plist・SQLite・shell履歴・Gitへ保存しない。

| account | 内容 | 一時override |
|---|---|---|
| `iphone-screen-time-pseudonym-key-hex` | 32 bytes以上の疑似化secretのhex | `PDP_IPHONE_SCREEN_TIME_PSEUDONYM_KEY_HEX` |
| `iphone-screen-time-motherduck-token` | 本番MotherDuck writer token | `PDP_IPHONE_SCREEN_TIME_MOTHERDUCK_TOKEN` |

疑似化secretを変更すると過去のdevice / segment keyとの同一性を失う。
接続先は`MOTHERDUCK_DATABASE`。Collectorは汎用`MOTHERDUCK_TOKEN`を使わない。

| 設定 | 既定値・内容 |
|---|---|
| `PDP_IPHONE_SCREEN_TIME_STATE_DB_PATH` | `~/Library/Application Support/personal-data-platform/iphone_screen_time/collector.db` |
| `PDP_IPHONE_SCREEN_TIME_SYNC_DB_PATH` | `~/Library/Biome/sync/sync.db` |
| `PDP_IPHONE_SCREEN_TIME_APP_IN_FOCUS_REMOTE_DIR` | `~/Library/Biome/streams/restricted/App.InFocus/remote` |
| `PDP_IPHONE_SCREEN_TIME_DEVICE_ALLOWLIST` | `devices`で確認した疑似化key（collect時に必須） |
| `PDP_IPHONE_SCREEN_TIME_POLL_SECONDS` | `1800`（最小10秒） |

`doctor`はBiomeのread access、端末設定、SQLite directoryのwrite可否、専用tokenとDB設定を確認する。
実際の接続・Raw decode・DB取込は`collect --once`で確認する。GCS設定・Collector ADCは不要である。

## LaunchAgent

```bash
export MOTHERDUCK_DATABASE="personal_data_platform"
export PDP_IPHONE_SCREEN_TIME_DEVICE_ALLOWLIST="<device_key>"
export PDP_IPHONE_SCREEN_TIME_POLL_SECONDS="1800"
collector_plist="$HOME/Library/LaunchAgents/com.personal-data-platform.iphone-screen-time-collector.plist"
pdp iphone-screen-time doctor
pdp iphone-screen-time collect --once
pdp iphone-screen-time launch-agent \
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
launchctl print "gui/$(id -u)/com.personal-data-platform.iphone-screen-time-collector"
tail -F "$HOME/Library/Logs/personal-data-platform/iphone-screen-time-collector.stdout.log" \
  "$HOME/Library/Logs/personal-data-platform/iphone-screen-time-collector.stderr.log"
```

設定変更・token更新・Rebuild前は先に停止する。

```bash
launchctl bootout "gui/$(id -u)" "$collector_plist"
```

## LoaderとRebuild

Collectorが通常の取り込みを行う。手動の`pdp loader --source iphone_screen_time --stream app-in-focus`と
`pdp rebuild --source iphone_screen_time --stream app-in-focus`も同じローカルRawを読む。
Rebuild中はCollectorを停止し、inventory取得から再生完了までRawを固定する。
別の空scratch DB、`--allow-partial-history`、dbt検証などは[Platform運用](../../platform/operations.md#rebuild)に従う。
`pdp dbt --source iphone_screen_time`はこのsourceのモデルだけを作成・検証する。

イベント・補助状態・台帳は同じ時点でバックアップ・復元する。保存するのは各ファイルの最新版であり、
更新前Rawによる再解析とRawだけからの全履歴復元は保証しない。
旧共通DBからの一度限りの移行は[分割切替](../../platform/operations.md#screen-timeの分割切替)に従う。
