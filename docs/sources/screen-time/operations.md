# 運用

## Collector state

```text
~/Library/Application Support/personal-data-platform/collector.db
```

DBはmode `0600`、専用directoryは`0700`。Rawと取込状態の保持範囲は[データモデル](data-model.md#observation-semantics)を参照する。

## 初回scanとwatch

```text
pdp screen-time collect --once   全対象をローカル保存してから直接取り込み
pdp screen-time collect --watch  同じ処理を1800秒間隔で反復
```

全対象を[取得仕様](acquisition.md#segb-container)に従って保存してからLoaderを実行する。
保存だけ成功しても取込成功とはみなさず、JSONの`loaded`と`pending`を確認する。
不完全snapshotの回も保存済みpendingは取り込めるが、成功heartbeatは更新しない。
追加DDLやCollectorのコード変更は、停止・更新後の再起動で反映する。

`--watch`は通信障害・lease競合・走査失敗をstderrへ記録し、1800秒後に再試行する。
`PDP_COLLECTOR_POLL_SECONDS`の既定値は1800、最小10秒。通常運用は1800秒を維持する。

## Crash recoveryと監視

再起動後もpendingの同じkey・bytesで再試行し、commit結果が不明な場合は再接続後にMotherDuck台帳を確認する。
片方のstreamが走査失敗しても他方を走査するが、その回を全体の成功として記録しない。

stream・端末別の最終scan時刻を[SQLiteへ保存する](data-model.md#collector-scan)。未設定streamは有効なdevice集合を空にして明示的に休止する。
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
更新前Rawによる再解析とRawだけからの全履歴復元は保証しない。
