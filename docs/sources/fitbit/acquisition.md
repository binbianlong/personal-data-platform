# Fitbit取得仕様

## Google Health API

OAuth refresh tokenからaccess tokenを得て、Google Health v4のreconcileを`google-wearables`指定で呼ぶ。
日次はTokyoの2日前00:00から当日00:00（排他）までの直近2完了日を、1日・1種別の計10取得単位へ分割して取得する。
心拍・歩数の通知は通知された範囲を取得し、その他の通知は日単位で取得する。
環境変数と長期停止後の期間指定は[運用](operations.md)を参照する。同期checkpointは保存しない。

| data type | フィルターに使う値 | 置換cursor |
|---|---|---|
| `steps` | 部分通知・日次・手動は`steps.interval.start_time`、全日通知は`steps.interval.civil_start_time` | UTC時刻または提供元の日付 |
| `heart-rate` | `heart_rate.sample_time.physical_time` | サンプルのUTC時刻 |
| `daily-resting-heart-rate` | `daily_resting_heart_rate.date` | 提供元の日付 |
| `active-zone-minutes` | `active_zone_minutes.interval.start_time` | 区間開始のUTC時刻 |
| `sleep` | `sleep.interval.civil_end_time` | 提供元の睡眠終了日付 |

日次指標・睡眠と歩数の全日通知はcivil date、それ以外はphysical timeで、
同じフィールドへの`>=`と`<`による半開区間の全ページを取得する。
page sizeは通常10,000、睡眠25。クライアントは心拍を14日、その他を90日に分割するが、APIの最大期間を保証しない。
1取得は1,000ページ・250,000正規化行・20分まで。超過時は失敗として範囲を狭めて再実行する。

401/403・OAuth失敗、429、通信/5xx、データ不正を別例外にする。レスポンス本文やtokenはエラーに含めない。
重複・循環page token、範囲外の行、途中ページ失敗を完全な取得とみなさない。
空の完全取得は削除を意味する。失敗時は既存データを削除しない。

歩数は取得済みの行と開始・終了、値、offset、現地日付、categoryを比較し、
新規・変更行だけを投入する。部分取得の後に同じ内容を全日取得しても、同じ行を再投入しない。
一致する行の保存メタデータは保持し、最新の取得確認はcoverageへ記録する。
取得範囲から消えた行は削除し、古い結果からの保護とトランザクションを維持する。
ID照会にはIDだけを渡し、健康値本体を照会パラメーターへ含めない。

point IDは`dataPointName`/`name`、主睡眠フラグは`mainSleep`/`main`を受理する。同時に存在して矛盾する場合は停止する。

## WebhookとPub/Sub

`POST /webhooks/fitbit`でAuthorizationの共有値とGoogle HealthのTink署名を検証する。
署名鍵は公式keysetから取得してcacheし、署名不一致時には一度だけ鍵を再取得する。
購読の検証は認証後に201、通常通知は全件検証・Pub/Sub発行後に204を返す。
部分的な発行失敗は503として再送を受ける。再配信は[範囲置換と取得順位](data-model.md#範囲置換と再実行)で処理する。

1 MiBまでのbodyを、1日・1種別の最大1,000単位へ分割する。
歩数の物理区間がある通知はUTCの半開区間をそのまま取得する。
小数秒は区間を狭めないよう保存精度のマイクロ秒へ外側に丸める。
物理区間がない全日通知は通知された現地暦日だけを取得し、UTCの複数日へ広げない。
どちらもschema 2の`acquisition_mode="bounded_steps"`で、`windows[].time_basis`に
`utc`または`civil`を指定する。同じ元通知内でも異なる時刻基準の区間は統合しない。
workerは要求範囲と基準が一致する完全な取得をCOMMITできた場合だけackする。
同じHTTP受信に歩数の元通知が2件以上ある場合、UTC同士・civil同士の重複や隣接区間を統合する。
UTCはTokyoの日境界、civilは提供元の日付境界で再分割し、各範囲を個別のschema 2メッセージとして発行する。
全日通知とUTC部分通知はそれぞれ取得する。receiverでの統合は同じHTTP受信内の歩数に限定する。
取得範囲ごとに再試行できるため、他の範囲が時間切れになっても完了済みのメッセージは再取得しない。
元の最大1,000単位の検証は統合前に行う。
物理区間がある心拍通知は元の小数秒精度で区間を検証し、UTCの開始を分境界へ切り下げ、
終了を切り上げる。元区間は診断ログに残す。
Pub/Subのschema 1へ任意の`acquisition_mode="bounded_minutes"`を付けて渡す。
指定は分境界に整列した心拍だけに許可し、省略時は従来の`legacy`取得を使う。
物理区間のない心拍、または物理区間あり・なしが混在する元通知は従来の保守的範囲を使う。
旧workerは追加項目を無視して日単位へ拡大するため、同じ通知形式を受理できる。
対応workerは`bounded_minutes`のキュー範囲を日全体へ拡大せず、その範囲だけ取得・置換する。
最後のUTC分が未完了ならAPI・DB接続前に503で保留し、分の完了後にPub/Subから再配信を受ける。
日境界をまたぐ未完了分もキューに残す。APIが要求より短い完了範囲を返した場合は書き込まず再試行する。
日次・手動取得と、`acquisition_mode`がない既存キュー通知の取得範囲は従来どおりとなる。
`fitbit_delivery_mode=pull`では全種類を既存Pub/Subへ保持し、15分ごとに非公開workerの
`/notifications/fitbit/drain`を起動する。通常の追加待機は0〜15分と処理時間で、
混雑・失敗・lease競合時は次回以降へ持ち越す。OwnTracks・Screen Timeはこの待機に含めない。
bounded歩数・心拍は、同じ取得モード・時刻基準・取得日内の重複と隣接範囲を統合する。
睡眠・安静時心拍とlegacy通知は元の範囲が完全一致する重複だけをまとめる。
未完了分を含むbounded心拍通知は完成済み通知と統合せず保留し、legacy心拍の全日取得は維持する。
収集は100件ずつ、合計1,000件・保持データ8MiB・10秒で制限する。
各RPCは最大2秒・自動再試行なし。空応答はbacklogがないことの保証ではない。
非空バッチだけDBへ接続し、migration検証とloader leaseを共有する。transactionは範囲ごとに確定し、
元通知の全範囲が成功したものだけACKする。失敗・上限超過・不正通知は未ACKで保持する。
全体480秒のうち20秒をACK・後始末に残し、COMMIT結果不明では接続を破棄して後続を止める。

`fitbit_delivery_mode=push`は切り戻し用の即時配信経路として維持する。
1通知を480秒の処理予算で取得し、正規化データとcoverageのDB commitを確認してから204を返す。
サービスtimeoutは540秒、ack期限とworkerのleaseは600秒。API・DB失敗、停止中、lease競合は503、
不正通知は400を返し、Pub/Subの10〜600秒のbackoffによる再配信に任せる。
日次処理との競合中は取得を延期する。commit結果不明の接続では後続書込を止める。
APIレスポンスはメモリで扱い、GCS・ローカルファイル・DBへRawとして保存しない。
通知の保持は7日。処理停止中もreceiverは発行を続ける。

## 参考仕様

- [Webhook](https://developers.google.com/health/webhooks)
- [reconcile API](https://developers.google.com/health/reference/rest/v4/users.dataTypes.dataPoints/reconcile)
- [日時フィルター](https://developers.google.com/health/filters)
- [データの統合方針](https://developers.google.com/health/data-management)
- [睡眠データ](https://developers.google.com/health/data-types/sleep)
