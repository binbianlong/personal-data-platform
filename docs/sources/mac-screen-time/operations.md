# Mac Screen Timeの運用

## Collector state

```text
~/Library/Application Support/personal-data-platform/mac_screen_time/collector.db
```

DBはmode `0600`、専用directoryは`0700`。Rawと取込状態の保持範囲は[データモデル](data-model.md#observation-semantics)を参照する。

## 初回scanとwatch

```bash
pdp mac-screen-time collect --once
pdp mac-screen-time collect --watch
```

初回の空base確認または参照移行が完了した状態で、各snapshotを外部pendingファイルへ保存してから送信する。
JSONの`event="screen_time_collection"`、`result`、`stage`、`loaded`と、SQLiteのpendingを確認する。
初期化済みなら接続障害・lease競合中も取得を続け、`--watch`で順に再送する。
同じsourceのprocess lockと、他sourceと共有するDBのloader leaseを使う。

## Crash recoveryと監視

再起動後はpendingの同じkey・bytesで再試行する。COMMIT確認後にだけSQLiteをACKし、payloadを削除する。
成功不明のpendingを互換性のないparserで再解釈しない。解析に失敗しDB未試行のbytesは、対応parserで再試行できる。
端末の設定解除や元ファイル消失で過去のbaseイベントは削除しない。設定変更はLaunchAgentの再生成・再起動で反映する。

このsourceの稼働確認はMacのCollectorログを使う。MotherDuckのsource heartbeatは持たず、日次JobもMacの鮮度を判定しない。
完全な空スキャンは成功し、新しいアプリ利用がないことだけでは障害とみなさない。
ファイル永続化後・pending登録前の停止で残った未参照spoolは、送信済みと証明できないため自動削除しない。

## 設定と診断

```bash
pdp mac-screen-time devices
pdp mac-screen-time doctor
```

Keychain service `personal-data-platform`に次の専用項目を登録する。tokenはKeychain Accessなどから設定し、
plist・SQLite・shell履歴・Gitへ保存しない。

| account | 内容 | 一時override |
|---|---|---|
| `mac-screen-time-pseudonym-key-hex` | 32 bytes以上の疑似化secretのhex | `PDP_MAC_SCREEN_TIME_PSEUDONYM_KEY_HEX` |
| `mac-screen-time-motherduck-token` | 本番MotherDuck writer token | `PDP_MAC_SCREEN_TIME_MOTHERDUCK_TOKEN` |

疑似化secretを変更すると過去のdevice / segment keyとの同一性を失う。
接続先は`MOTHERDUCK_DATABASE`。Collectorは汎用`MOTHERDUCK_TOKEN`を使わない。

| 設定 | 既定値・内容 |
|---|---|
| `PDP_MAC_SCREEN_TIME_STATE_DB_PATH` | `~/Library/Application Support/personal-data-platform/mac_screen_time/collector.db` |
| `PDP_MAC_SCREEN_TIME_SYNC_DB_PATH` | `~/Library/Biome/sync/sync.db` |
| `PDP_MAC_SCREEN_TIME_APP_USAGE_LOCAL_DIR` | `~/Library/Biome/streams/restricted/ScreenTime.AppUsage/local` |
| `PDP_MAC_SCREEN_TIME_DEVICE_KEY` | `devices`で確認した疑似化key（collect時に必須） |
| `PDP_MAC_SCREEN_TIME_POLL_SECONDS` | `1800`（最小10秒） |

`doctor`はBiomeのread access、端末設定、SQLite directoryのwrite可否、専用tokenとDB設定を確認する。
実際の接続・Raw decode・DB取込は`collect --once`で確認する。GCS設定・Collector ADCは不要である。

## LaunchAgent

```bash
export MOTHERDUCK_DATABASE="personal_data_platform"
export PDP_MAC_SCREEN_TIME_DEVICE_KEY="<device_key>"
export PDP_MAC_SCREEN_TIME_POLL_SECONDS="1800"
collector_plist="$HOME/Library/LaunchAgents/com.personal-data-platform.mac-screen-time-collector.plist"
pdp mac-screen-time doctor
pdp mac-screen-time collect --once
pdp mac-screen-time launch-agent \
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
launchctl print "gui/$(id -u)/com.personal-data-platform.mac-screen-time-collector"
tail -F "$HOME/Library/Logs/personal-data-platform/mac-screen-time-collector.stdout.log" \
  "$HOME/Library/Logs/personal-data-platform/mac-screen-time-collector.stderr.log"
```

設定変更・token更新・移行前は先に停止する。

```bash
launchctl bootout "gui/$(id -u)" "$collector_plist"
```

## LoaderとRebuild

通常の取り込みと再送はCollectorが行う。汎用object loaderはOwnTracks用であり、このsourceのpending状態を代替しない。
`pdp dbt --source mac_screen_time`はこのsourceの区間・日次ビューを作成・検証する。

送信済みのRawは残さず、全履歴の正本はMotherDuckのbase。pendingだけによる全履歴rebuildは拒否する。
DB移行ではbaseのイベント・有効状態とSQLiteの参照を引き継ぎ、両Collectorを停止して検証する。
旧SQLiteを新しいstateとして空初期化せず、[分析スキーマ移行](../../platform/analytics-migration.md)に従う。
