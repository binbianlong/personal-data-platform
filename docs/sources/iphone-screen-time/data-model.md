# iPhone Screen Timeのデータモデル

## Local pendingと参照

専用SQLite `collector.db`の永続表は`file_state`と`event_reference`の2つ。
送信待ちのgzip envelopeは、隣接する`collector.payloads` directoryの外部ファイルへ保存する。
SQLiteにRaw BLOBや送信済み全履歴は持たない。snapshot keyの形式は次のとおり。

```text
raw/iphone_screen_time/v2/<device_key>/app-in-focus/<segment_key>/<observed_at>/<sha256>.segb.gz
```

hashはgzip前envelopeのSHA-256。SQLiteの`observed_at`は取得UTC時刻のISO 8601表現。
`device_key`は端末identifier、`segment_key`は端末・stream・相対pathをHMAC-SHA-256で疑似化する。

## Observation semantics

`file_state`はsnapshot key、segment、端末、hash、取得時刻、parser版、pending/sent、payload pathを保持する。
`event_reference`はsegmentと採用済みイベントIDの対応。最新sent metadataと全pendingを保持する。
同じ内容・parserの連続sentだけを省略し、A→B→Aはそれぞれ取り込む。

処理順序はpayloadの永続化→pending記録→全件DB COMMIT確認→SQLiteの参照更新とsent化→ファイル削除。
COMMIT不明やACK失敗では同じbytesとIDを再送し、後続snapshotは追い越さない。
解析に成功した後、最初のDB試行前にparserを固定する。未知parserで失敗したbytesは対応版で再解析できる。
DB試行済みのpendingを互換性のないparserで暗黙再解釈しない。

新規SQLiteのuser_version=0では、共有lease内でこのsourceのbaseが空と確認してからuser_version=1にして取得を始める。
既存baseへ接続する場合は、整合する参照と初期化済み状態（user_version=1）を備えたSQLiteが必要。
検証済みのuser_version=1では、DB障害中もローカル取得を続けてpendingを残す。
ファイル永続化後・pending登録前のクラッシュで生じた未参照ファイルは、自動削除せず手動で確認する。

## `base.iphone_screen_time_event`

カラムは`event_key`, `device_key`, `bundle_id`, `event_at`, `state`, `is_valid`。
端末・stream・Bundle ID・元の時刻・開始/終了・kindからイベントを識別し、segmentやoffsetはIDに含めない。
同じイベントは1行。`is_valid=false`で分析から除き、履歴の行は残す。

元ファイルの消失、TTL、別内容snapshotでの省略だけでは無効化しない。
同一bytesのparser訂正で除外されたイベントで、ほかのsegment参照がない場合にだけ無効化する。
source専用のMotherDuck opsやheartbeatはない。

## 利用区間と日次合計

`marts.iphone_screen_time_interval`は有効イベントを端末内で対応付け、正の長さの区間だけ公開する。
同じappのendが次のstart以前なら観測区間。endより先に次のstartがあれば推定区間で、`is_inferred=true`。
`boundary_event_key`が終了の根拠を示す。対応できない記録はbaseに残る。

`marts.daily_iphone_screen_time_total`はJST日につき1行。同じOSの全端末の重複区間を和集合にし、観測を推定より優先する。
`observed_seconds`、追加分の`inferred_seconds`、両者の`total_seconds`、`unresolved_event_count`を返す。
iPhoneとMacは独立。未解決イベントだけの日は秒数NULL、記録も区間もない日は行を作らない。当日は途中経過。
小数秒を保持し、午前0時で終わる区間から翌日の0秒片は作らない。訂正はビューへ即時反映する。

全履歴の正本はbase。pendingだけを使った全履歴rebuildは行わず、DB移行ではbaseと対応するSQLite参照を引き継ぐ。
全カラムは[基礎スキーマ](../../platform/data-model.md)を参照する。
