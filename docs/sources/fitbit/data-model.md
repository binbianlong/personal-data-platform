# Fitbitデータモデル

## APIからの直接取込

全ページ取得を検証し、正規化データと`ops.fitbit_coverage`を同じtransactionへ書き込む。
`fetched_at`は全ページ取得の開始時刻。`source_key`は`fitbit-api:<UUID>`形式の取得識別子で、保存先を表さない。
Rawや同期checkpointは保存せず、復旧には期間指定のAPI再取得を使う。

## テーブルと単位

| base table | 保存する意味 |
|---|---|
| `fitbit_activity_interval` | 歩数と重み付け済みAZMの区間 |
| `fitbit_resting_heart_rate_daily` | 提供元の日付ごとの安静時心拍 |
| `fitbit_sleep_session` | 睡眠区間とAPI summaryの睡眠分数 |
| `fitbit_sleep_detail` | 睡眠段階と短い覚醒。重なりを保持する |
| `fitbit_heart_rate_minute` | UTC分ごとの平均・最小・最大bpm |
| `fitbit_hrv_daily` | 提供元日付の平均・深睡眠RMSSD（ms）、non-REM心拍、entropy |
| `fitbit_hrv_sample` | 元の測定時刻とRMSSD・SDNN（ms） |
| `fitbit_respiratory_rate_daily` | 提供元日付の主睡眠呼吸数（回/分） |
| `fitbit_respiratory_rate_sleep_summary` | 元の測定時刻と睡眠全体・deep/light/REMの呼吸数・標準偏差・SNR |
| `fitbit_oxygen_saturation_daily` | 提供元日付のSpO2平均・信頼区間・標準偏差（%） |
| `fitbit_sleep_temperature_daily` | 提供元日付の夜間皮膚温・基準温と相対夜間温度の30日標準偏差（℃） |
| `fitbit_exercise` | 種類・表示名、実開始/終了、提供元開始日、休止を除く活動秒数 |
| `fitbit_vo2_max_daily` | 提供元日付のVO2 max（ml/kg/min）、推定フラグ、fitness level、covariance |
| `fitbit_sedentary_period` | 装着中に動いていない実区間 |
| `fitbit_distance_interval` | 元の実区間と距離（m） |
| `fitbit_heart_rate_zone_interval` | 元の実区間と提供元のzone文字列 |

日次指標と睡眠・運動の取得範囲にはproviderのDATEを使い、物理時刻の開始として扱わない。
日次テーブルはsubject・提供元日付で1行。睡眠は終了日、運動は開始日を取得cursorとし、元の実時刻も保存する。
HRV・睡眠呼吸summaryはsampleのまま保持し、区間や睡眠IDとの対応を推測しない。
睡眠詳細の日付と物理区間は親セッションに従う。睡眠分数はAPI summaryを使い、区間の経過分数で代用しない。
分心拍の終了は開始から1分後。APIで不明な測定回数は保存しない。

`fitbit_sleep_session`にはAPI summaryの`minutes_awake`・`minutes_in_sleep_period`・
`minutes_to_fall_asleep`・`minutes_after_wake_up`も保存する。欠けた値はNULL、返却された0は0。
自動検出の入眠時間0はAPIの既定値になり得るため、実測で即入眠したとは断定しない。
既存の履歴に追加列を埋めるための一括再取得は行わない。

運動の取得元は`google-sources`、他の型は`google-wearables`で固定し、coverageにも同じfamilyを使う。
未知の運動種類・zone文字列も保持する。SpO2の上下値は信頼区間であり、観測最小・最大ではない。

## 範囲置換と再実行

全ページ取得済みの範囲だけを置き換え、空結果も反映する。
coverageはsubject・種別・data source family・aggregation versionごとに管理し、
`(fetched_at, source_key)`順で新しい取得を保護する。古い取得は未反映部分だけを更新する。
歩数はUTCの`records-v1`と暦日の`records-civil-v1`の両方を照合する。

同一範囲の内容が現在と同じなら書き換えを省いて取得順位を進めるため、保存行の取得時刻は最新の照合時刻とは限らない。
A→B→Aも反映し、空取得後の古い結果による復活を防ぐ。
睡眠IDの日付移動では子も親ID単位で置換する。`ops.fitbit_deleted_record`に削除IDの取得順位を残し、
より新しい取得による復帰だけを許可する。
COMMIT結果が不明な場合は次の通知または期間指定同期で再取得する。

## 分析ビュー

`daily_fitbit_health`は歩数・心拍・AZM・睡眠に加え、HRV・呼吸数・SpO2・皮膚温変化・VO2 max・
距離・観測不活動秒数・zone別実滞在秒数を返す。`daily_fitbit_heart_rate_minute`は分心拍の日次集計を返す。
提供元の日次値はsampleから再計算しない。各入力を1人・1日へ揃えてからjoinし、日次行を増殖させない。
区間・分心拍・睡眠の詳細は`fitbit_steps_time_series`、`fitbit_heart_rate_minute_time_series`、`fitbit_sleep_sessions`で参照する。

`fitbit_exercise_sessions`は運動の元の種類・表示名・開始/終了を返し、提供元の`active_duration_seconds`と
開始から終了までの`elapsed_seconds`を分ける。前者が欠けていても経過時間で補わない。

区間の日付比較はAsia/Tokyo。歩数・AZM・距離は日境界で経過時間に比例して分配するため小数になり得る。
不活動・zone時間は日境界で分割した実区間長を使い、欠測・非装着を不活動へ変換しない。
zoneは`LIGHT`・`MODERATE`・`VIGOROUS`・`PEAK`の列へ集計し、該当区間がなければNULLとする。
皮膚温変化は夜間値−基準値で、負値を許す。基準値がなければNULL。VO2 maxの欠けた日付も補間しない。

日次指標はAPI提供日付を維持し、睡眠の分析日付はTokyoでの終了日、運動は開始日とする。
提供元日付とTokyo日付は旅行時などに異なり得る。睡眠・運動の元日付は別列に残す。
欠測を0へ変換しない。睡眠時間に段階と短い覚醒を加算せず、AZMへの追加重み付けもしない。

`fitbit_sleep_iphone_screen_time`と`fitbit_sleep_mac_screen_time`は睡眠開始前2時間のScreen Timeを端末別に返す。
completeと次の開始から終了を推定した区間を窓内で切り詰め、端末内の重複を統合する。
対象区間がない端末は行を作らず、端末横断で合算しない。同一個人の端末を前提とし、複数人のデータを混在させない。
