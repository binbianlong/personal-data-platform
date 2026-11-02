# Fitbitデータモデル

## APIからの直接取込

共有leaseを取得してからAPIの全ページを検証し、取得できた種別・範囲を1 transactionで更新する。
APIレスポンスはメモリだけで扱う。COMMIT不明時は接続を破棄し、通知再配信や期間指定同期でAPIから取り直す。
保存済みの古いsnapshotを後から適用する方式は使わない。

## 保存する12表

| base table | 保存する意味 |
|---|---|
| `sleep_session` | 睡眠1回、提供元の実睡眠分数・覚醒・睡眠期間・入眠・起床後分数 |
| `sleep_detail` | 親睡眠に属する睡眠段階・短い覚醒の区間。kindと分類を保持 |
| `heart_rate_minute` | UTC分ごとの平均・最小・最大bpmと集計方式 |
| `steps_interval` | 元の区間と歩数増分 |
| `active_zone_interval` | 元の区間と提供元で重み付け済みのAZM |
| `exercise_session` | 運動種類・表示名、実時刻、休止を除く活動秒数 |
| `sedentary_interval` | 提供元が不活動と報告した実区間 |
| `distance_interval` | 元の実区間と距離m |
| `heart_rate_zone_interval` | 元の実区間と提供元のzone文字列 |
| `health_daily` | 提供元日付ごとの安静時心拍・HRV・呼吸数・SpO2・皮膚温・VO2 max |
| `hrv_sample` | 元の測定時刻とRMSSD・SDNN（ms） |
| `sleep_respiratory_summary` | 元の測定時刻と全体・deep/light/REMの呼吸数・標準偏差・SNR |

全カラムは[基礎スキーマ](../../platform/data-model.md)とDBのCOMMENTを参照する。
単一の設定済みアカウントを扱い、baseにはaccount・取得時刻・取得履歴を持たない。
運動はgoogle-sources、他の型はgoogle-wearablesから取得する。未知の運動種類・zone・分類も保持する。

提供元の日付は`source_date`のまま保存する。睡眠は終了日、運動は開始日を取得cursorに使う。
分心拍の終了は開始の1分後。不明な測定回数や、提供元にないsampleの終了時刻・睡眠IDは作らない。
実睡眠分数はAPI summaryを使い、開始と終了の差で代用しない。自動検出の入眠0は既定値の場合がある。
運動の`active_seconds`は経過時間と異なり、未提供ならNULL。SpO2上下限は信頼区間であり観測最小・最大ではない。
SNRは確率や%として扱わない。欠けた値と返却された0を区別する。

## 範囲置換と日次値

取得失敗では保存済みの値を変えない。正常な完全取得の空結果は反映する。
歩数はUTCなら物理開始時刻、civilなら提供元日付を使い、取得cursorと同じ基準で置換する。
睡眠の移動・訂正は親IDに対応する子も同じtransactionで更新する。実値が同じ行は書換えを省く。

`health_daily`は日付につき1行。取得した指標の列だけ上書きし、正常な空結果ではその指標の列だけNULLにする。
ほかの指標を消さず、全指標がNULLになった日だけ行を取り除く。
記録IDの日付移動を追跡する表はないため、取得範囲外の日付訂正は、その日を再取得するまで反映されない。
日次自動補修の直近2完了日より古い範囲は、必要な期間を指定して再取得する。

## 日次ビュー

- `marts.daily_steps`: JST日境界で時間比例配分した歩数。分割片数と配分有無を保持する。
- `marts.daily_active_zone`: 同様に配分したAZM。既に重み付け済みなので強度を再度掛けない。
- `marts.daily_sleep_detail`: 親睡眠の終了JST日×kind×categoryごとの区間和集合分数。昼寝を含め、種類をまたいで足さない。

提供元日次値はsampleから再計算しない。provider日付とJST日付は旅行時などに異なる。
区間配分は小数になり得る。未観測日を0で補完せず、非装着や欠測を不活動へ変換しない。
