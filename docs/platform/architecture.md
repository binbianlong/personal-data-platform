# アーキテクチャ

行動・健康の分析履歴はMotherDuckの`personal_data_platform`へ保存する。分析用shareは`pdp_analytics`。

```text
iPhone Collector -> 専用SQLite + pendingファイル -> base.iphone_screen_time_event
Mac Collector    -> 専用SQLite + pendingファイル -> base.mac_screen_time_event
OwnTracks -> receiver -> GCS Raw -> Pub/Sub -> worker -> base.location_point
Google Health Webhook -> receiver -> Pub/Sub -> worker -> API完全取得 -> base

base 18物理表 -> dbt marts 7ビュー -> read-only share / Remote MCP
日次Job -> Fitbit直近2日補修 -> dbt -> OwnTracks差分修復 -> relation検査 -> Healthchecks
実行結果 -> JSONログ
```

## データの配置

| 配置 | 所有者・内容 |
|---|---|
| `base` | Pythonのsource writerが保存する提供元の記録・日次値・手動設定。18物理表 |
| `marts` | dbtが定義する利用区間・日次集計。7ビューで、元データの変更をquery時に反映する |
| `ops.job_lock` | 全writerが共有する期限付きlease |
| `ops.schema_migration` | 適用済みSQLとchecksum |
| 各CollectorのSQLite | `file_state`と`event_reference`。送信待ちと確定済み参照 |
| GCS | OwnTracksの不変Raw。無期限保持 |

カラム・単位・NULL・日付の定義は[データモデル](data-model.md)と各table/columnのCOMMENTを参照する。
coreレイヤーやsource別の取り込み台帳は持たない。本人設定と場所は手動登録し、推測で埋めない。

## 保存と再試行

共通Warehouseは接続・transaction・leaseを所有し、型付きbatchからsource固有の保存処理を呼ぶ。
各sourceのSQLは`src/personal_data_platform/sources/<source>/motherduck.py`に置き、source writer自身はcommitしない。

leaseはAPI取得前から保存まで保持する。書込みtransactionでは所有者と期限を条件に同じlease行へ実際に書き込み、
別所有者への引き継ぎと競合させる。初回の空DB作成以外のDDLも同じleaseに従う。
COMMIT結果不明やROLLBACK失敗では接続を再利用せず、後続の保存・ACKを止める。

| source | 再試行・復旧 |
|---|---|
| OwnTracks | Rawの端末・内容から作る決定的IDで再送を判定。GCSとbase IDを照合し欠落を修復する |
| Fitbit | lease内でAPIを取得し直す。完全取得できた種別・範囲だけ更新し、古い保存済みレスポンスは再適用しない |
| iPhone / Mac | pendingの同じbytes・IDを順番に再送する。DB確定→SQLite ACK/参照更新→ファイル削除 |

OwnTracksのparser変更は`loader --reprocess`で対象範囲を明示する。通常のID照合はparser版を記録しない。
Screen Timeの送信完了bytesは残さず、元ファイルの消失だけでbase履歴を無効化しない。
全履歴の移行ではbaseをコピーする。FitbitはAPIで再取得可能な範囲、OwnTracksは保存済みGCS Rawからも復元できる。

## Sourceの分離

`iphone_screen_time`と`mac_screen_time`は別の取得処理・設定・SQLite・base table・LaunchAgentを持つ。
`biome/`はSEGB、envelope、疑似化、pending state、収集engineの共通処理を持つ。
片方の取得・送信失敗は他方のローカル状態を変更しない。DBへの書込みは共通leaseで直列化する。

FitbitはRaw adapterへ登録せず、API取得したsnapshotを直接保存する。新しいsourceには取得・型付きbatch・source writer、
必要なbase定義、selectorと必須relation、再試行テストを追加する。適用済みmigrationは変更せず、変更分のSQLを追記する。

## 運用境界

日次・sourceの結果はログで確認する。日次成功は外部Healthchecksの応答成功後だけ記録し、競合延期では成功pingを送らない。
Macの稼働確認はCollectorログを使い、最後のアプリ利用時刻を稼働時刻と解釈しない。
Cloud Run失敗・Pub/Sub滞留・receiver/worker ERRORの監視は継続する。

[運用](operations.md)、[セキュリティ](security.md)、[旧スキーマからの移行](analytics-migration.md)を参照する。
保存先を追加する場合はWarehouse、source SQL、DDL、dbtに加え、その保存先での排他・確定・訂正反映を検証する。
