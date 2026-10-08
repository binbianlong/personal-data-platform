# Screen TimeローカルRaw切替 — 2026-10-08

Screen Timeの保存先をGCSからMacの既存`collector.db`へ切り替えた。MacのCollectorがローカル保存後にMotherDuckへ直接取り込み、
Cloud Runの日次JobはMacの取込成功記録とDBを監査する。取得間隔は1800秒、DBスキーマとイベントkeyは変更していない。

## 移行

旧Collectorと両Schedulerを停止し、イベント・補助状態・集計の件数とhashを比較基準にした。
west bucketのRaw 61件と旧bucketのRaw 53件、両bucketのcontrol 8件をgeneration固定で読み、gzipとSHA-256を検証した。
未取込3件を既存Loaderで処理し、移行前のイベントkey 80,386件がすべて残り、新たに9,122件加わったことを確認した。

25元ファイルの最新版、gzip合計3,898,456 bytesを既存SQLiteへ移し、既存pendingを保護した。
最新segmentを収集した初回には29元ファイルの保存版を保持した。現在Biomeにないファイルの保存版も残している。
本番writer tokenは専用Keychain項目へ登録し、plistからGCS設定・ADC参照を外した。

## 検証

- pytest 621件成功、ruff・mypy成功。
- Terraform fmt/validateと変更対象の18ケース成功。applyは追加0・更新8・削除0、その後のplanは差分なし。
- linux/amd64コンテナのbuild・CLI・migration smoke成功。
- ローカルRaw全件のhash、production取込台帳のidentity/generation/parser version、現行segment headとの一致を確認した。
- `001_initial.sql`の適用済みchecksumを維持した。
- Cloud Run `reconciliation-west-tl7jp`が2026-10-08 20:51 JSTに成功。Fitbit補修、dbt 10 view・39 test、Macのheartbeat監査、外部成功pingを確認した。
- 日次Jobの実行前後でMacの両stream heartbeatは20:42 JSTのままで、日次heartbeatだけが更新された。
- 両Schedulerは`ENABLED`へ戻した。

本番imageは次のdigestを使用する。

```text
us-west1-docker.pkg.dev/health-data-pipeline-503813/personal-data-platform/personal-data-platform@sha256:5ad2fc6d03c28dea385ad8850fb895ebde3bb95c80229784b4ee33d116b4ddaa
```

旧imageは`deployed-screen-time-before-20261008`タグで保持した。

## 通常周期と撤去

最終コードで20:42 JSTの初回を基準に、21:12、21:43の通常watchを確認した。
scan間隔は1839.34秒、1836.52秒で、各回とも最新Raw 1件を保存・取り込み、`deferred=0`、`pending=0`だった。
各回の取込成功記録、Raw hash、現行segment head、本番台帳の一致を確認した。
初回時点の93,433イベントkeyは最終確認時もすべて残り、全体は93,556件となった。
毎時Job `fitbit-hourly-west-mvlw4`も21:16 JSTに成功した。

21:47 JSTに検証済みgenerationを指定してRaw 114件・control 8件、保存byte数14,519,299を削除した。
両bucketの`raw/screen_time/`は過去generationを含め0件となり、Fitbitとbucket自体は保持した。
旧専用Collector ADC、移行中のバックアップ・event hash一覧・一時スクリプト、今回のDocker image/cacheを整理した。

最終SQLiteは4,300,800 bytes（約4.1 MiB）で、Raw gzipは29元ファイル・4,175,222 bytes（約4.0 MiB）。
`pending=0`、payload欠損0、freelist 0、mode `0600`を確認した。
件数・比較hash・通常周期・撤去結果は4,842 bytesの検証記録にまとめた。

## 保存と復旧の範囲

残すRawは各元ファイルの最新成功版と未取込分だけである。更新前のRawによる再解析と、Rawだけからの全履歴復元は保証しない。
MotherDuckの既存履歴は保持する。RebuildはCollectorを停止してローカルRawを読む。
運用・再移行の手順は[Screen Time運用](operations.md)を参照する。
