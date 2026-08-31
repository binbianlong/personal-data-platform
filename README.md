# Personal Data Platform

個人データ基盤を構築するためのPythonプロジェクト。

## 開発環境

Python 3.13以降を使用する。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
```

CLIはローカルCollectorとCloud Run Jobの共通entrypointを提供する。

```bash
pdp --help
python -m personal_data_platform.entrypoint --help
```

主なcommand:

```text
pdp screen-time devices
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time collect --watch
pdp screen-time launch-agent --output <plist-path>
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-database>
pdp preflight
```

未実装の`webhook`と`fetch`はcommandとして受理しない。

## iPhone Screen Time Collector

CollectorはMacへ同期されたBiomeの`sync.db`から`platform = 2`のiPhoneを列挙し、allowlistに含まれるdeviceの`App.InFocus/remote` segmentをB2へ保存する。Full Disk AccessはTerminalではなく、実際にCollectorを起動するプロセスへ付与する。

疑似化secret、B2 key ID、B2 application keyはmacOS Keychain service `personal-data-platform`の次のaccountから読む。対応する環境変数があれば環境変数を優先する。

| Keychain account | 環境変数 |
|---|---|
| `screen-time-pseudonym-key-hex` | `PDP_PSEUDONYM_KEY_HEX` |
| `b2-key-id` | `B2_KEY_ID` |
| `b2-application-key` | `B2_APPLICATION_KEY` |

疑似化secretには32 bytes以上のhex値を使う。まずsecretをKeychainへ登録し、device候補を確認する。

```bash
pdp screen-time devices
```

出力された`device_key`のうち収集対象だけをカンマ区切りで`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`へ設定する。Raw device identifierはallowlistへ保存しない。

接続先には`B2_ENDPOINT`、`B2_BUCKET`を設定する。必要に応じて`B2_REGION`も設定できる。Collector用B2 application keyは対象bucketと`raw/screen_time/v1/` prefixに限定し、`writeFiles`だけを許可する。

```bash
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time collect --watch
```

`doctor`はBiome、allowlist、local state、B2設定を診断する。実際のB2書込権限は`collect --once`で検証する。

Raw object keyは次の形式で、device identifierとsegment pathはHMAC-SHA-256で疑似化する。SHA-256はgzip前のsegment bytesに対して計算する。

```text
raw/screen_time/v1/<device_key>/app-in-focus/<segment_key>/
  <YYYYMMDDTHHMMSSffffffZ>/<sha256>.segb.gz
```

local SQLite stateはupload前に同じobject keyと決定的gzip bytesを`pending`として保存し、B2 upload成功後だけ`uploaded`へ更新する。再起動時はB2のread/list権限を使わず、同じkeyとbytesでpending uploadを再試行する。連続する同一segmentはskipするが、`A → B → A`の観測は3件とも保持する。

`--watch`はdefaultで300秒ごとにcomplete scanを行う。間隔は`PDP_COLLECTOR_POLL_SECONDS`で変更できる。成功時は`raw/screen_time/v1/_control/collector/latest/`の疑似化receiptも更新し、cloud側がevent未発生とCollector停止を区別できるようにする。

常駐実行には、秘密値を含まないLaunchAgent plistを生成してからmacOSへ登録する。生成だけでは登録・起動されない。

```bash
pdp screen-time launch-agent \
  --output "$HOME/Library/LaunchAgents/com.personal-data-platform.screen-time-collector.plist" \
  --project-root "$(pwd)" \
  --python-executable "$(pwd)/.venv/bin/python"
```

Full Disk Accessの付与、plistの検証、登録・停止手順は[`Screen Time運用`](docs/sources/screen-time/operations.md)に従う。

## テスト

workflowの回帰テストには、Pythonに加えてBashとjqが必要になる。

```bash
ruff check src tests
ruff format --check src tests
pytest
```

## コンテナ実行

```bash
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev --help
```

## CI

Pull Requestと`main`へのpushでは、Python、コンテナ、Terraformの検証をGitHub Actionsで実行する。default branchへのマージ条件は[`infra/github/`](infra/github/)のrepository rulesetで管理する。

設計資料は[`docs/`](docs/)を参照する。
