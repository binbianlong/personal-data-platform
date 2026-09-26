# Personal Data Platform

個人データの取得、Raw保存、MotherDuckへの取込、dbt分析を行うPythonプロジェクト。

Loader、監査、再構築はsourceとstreamを選んで実行する。Screen TimeはiPhoneの`App.InFocus`と
Macの`ScreenTime.AppUsage`を扱う。Fitbitは未実装である。共通処理とsourceの分離・追加手順は
[`アーキテクチャ`](docs/platform/architecture.md)を参照する。

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
pdp screen-time inspect-mac
pdp screen-time collect --once
pdp screen-time collect --watch
pdp screen-time launch-agent --output <plist-path>
pdp loader
pdp dbt
pdp reconciliation
pdp rebuild --dry-run
pdp rebuild --target-db <scratch-database> --allow-partial-history
pdp preflight
```

`loader`、`reconciliation`、`rebuild`は指定を省略すると既存iPhoneの`screen_time / app-in-focus`を対象にする。
両端末を処理する場合は`--source screen_time --all-streams`、単一streamなら
`--source screen_time --stream app-in-focus`または`--stream app-usage`を付ける。
未登録の組合せは拒否する。`pdp dbt`は指定なしでは全model、source / stream指定時は対応するmodelとtestを実行する。

未実装の`webhook`と`fetch`はcommandとして受理しない。

`pdp screen-time inspect-mac`はMac自身の`App.InFocus/local`にある完成済みsegmentを読み取り専用で
解析する。`--directory PATH`で検証対象を変更できる。JSONにはBundle ID別の開始・終了レコード件数、
全体のrecord種別件数とevent時刻の範囲を表示する。これは利用時間の集計ではなく、RawやDBへの保存、
GCS認証、Keychainの設定は行わない。詳細は[`Screen Time取得仕様`](docs/sources/screen-time/acquisition.md#appinfocus診断command)を参照する。

## Screen Time Collector

CollectorはMacへ同期されたBiomeの`sync.db`から`platform = 2`のiPhoneを列挙し、allowlistに含まれるdeviceの`App.InFocus/remote` segmentをGCSへ保存する。Full Disk AccessはTerminalではなく、実際にCollectorを起動するプロセスへ付与する。
Mac自身は`platform = 3 AND me = 1`の唯一の行から同じsecretで疑似化し、
`PDP_SCREEN_TIME_MAC_DEVICE_KEY`を設定した場合に`ScreenTime.AppUsage/local`の完成済みsegmentを収集する。

疑似化secretはmacOS Keychain service `personal-data-platform`から読む。対応する環境変数があれば環境変数を
優先する。GCS認証には専用Collector Service AccountをimpersonateするADCを使う。

| Keychain account | 環境変数 |
|---|---|
| `screen-time-pseudonym-key-hex` | `PDP_PSEUDONYM_KEY_HEX` |

疑似化secretには32 bytes以上のhex値を使う。まずsecretをKeychainへ登録し、device候補を確認する。

```bash
pdp screen-time devices
```

出力された`device_key`のうち収集対象だけをカンマ区切りで`PDP_SCREEN_TIME_DEVICE_ALLOWLIST`へ設定する。Raw device identifierはallowlistへ保存しない。
Macを収集する場合は`devices`で`platform=macos`の`device_key`を確認し、
`PDP_SCREEN_TIME_MAC_DEVICE_KEY`へ設定する。両方の設定は同じCollectorで併用できる。

接続先には`GOOGLE_CLOUD_PROJECT`、`GCS_BUCKET`、`PDP_COLLECTOR_SERVICE_ACCOUNT_EMAIL`、
`GOOGLE_APPLICATION_CREDENTIALS`を設定する。Collector Service Accountは`raw/screen_time/v1/`へのcreateと
scan receipt・active-device manifestの更新だけを許可し、Rawのread / list / deleteを許可しない。

```bash
pdp screen-time doctor
pdp screen-time collect --once
pdp screen-time collect --watch
```

`doctor`はBiome、allowlist、local state、GCS設定、impersonated ADCの種類・所有者・mode・target
Service Accountを診断する。実際のtoken発行とGCS書込権限は`collect --once`で検証する。

Raw object keyは次の形式で、device identifierとsegment pathはHMAC-SHA-256で疑似化する。SHA-256はgzip前のRaw v2 envelope全体に対して計算する。

```text
raw/screen_time/v2/<device_key>/<app-in-focus または app-usage>/<segment_key>/
  <YYYYMMDDTHHMMSSffffffZ>/<sha256>.segb.gz
```

local SQLite stateはupload前に同じobject keyと決定的gzip bytesを`pending`として保存し、GCS upload成功後だけ`uploaded`へ更新する。再起動時は現在のallowlistから削除済みのdeviceも含め、GCSのread/list権限を使わずに同じkeyとbytesでpending uploadを再試行する。連続する同一segmentはskipするが、`A → B → A`の観測は3件とも保持する。

`--watch`はdefaultで300秒ごとにcomplete scanを行う。間隔は`PDP_COLLECTOR_POLL_SECONDS`で変更できる。成功時は
iPhoneの`raw/screen_time/v1/_control/collector/latest/`と`_control/collector/active.json`、Macの
`_control/collector/app-usage/latest/`と`_control/collector/app-usage/active.json`を別々に更新する。
端末の正式なdecommissionはallowlistまたはMac keyから削除した後、次のcomplete scanがmanifest更新まで
成功した時点とする。対象が0台になったstreamは空のmanifestで休止を示す。

`pdp rebuild`はCollectorのwrite-only ADCを使わない。Terraformが作るread-only Rebuild Service Account用の
別ADCを`PDP_REBUILD_GOOGLE_APPLICATION_CREDENTIALS`で指定する。作成手順は
[`Platform運用`](docs/platform/operations.md#rebuild)に従う。

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
mypy src
pytest
```

Python本体（`src`）はPython 3.13を対象にmypyのstrictモードで型チェックする。
テストコードは型チェック対象に含めず、pytestで動作を検証する。
GCS・SEGBの型情報がない依存は、利用する操作に絞ったProtocolで境界を定義する。
汎用SQLの結果とソース固有の詳細値の変換では、実行時に型が決まるため限定的にAnyを使用する。

## コンテナ実行

```bash
docker build --tag personal-data-platform:dev .
docker run --rm personal-data-platform:dev --help
```

## CI

Pull Requestと`main`へのpushでは、Python、コンテナ、Terraformの検証をGitHub Actionsで実行する。default branchへのマージ条件は[`infra/github/`](infra/github/)のrepository rulesetで管理する。

設計資料は[`docs/`](docs/)を参照する。
