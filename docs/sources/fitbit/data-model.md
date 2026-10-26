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

安静時心拍と睡眠の取得範囲にはproviderのDATEを使い、物理時刻の開始として扱わない。
睡眠詳細の日付と物理区間は親セッションに従う。睡眠分数はAPI summaryを使い、区間の経過分数で代用しない。
分心拍の終了は開始から1分後。APIで不明な測定回数は保存しない。

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

`daily_fitbit_health`は歩数・心拍・AZM・睡眠の日次集計、`daily_fitbit_heart_rate_minute`は分心拍の日次集計を返す。
区間・分心拍・睡眠の詳細は`fitbit_steps_time_series`、`fitbit_heart_rate_minute_time_series`、`fitbit_sleep_sessions`で参照する。

日付比較はAsia/Tokyo。歩数とAZMが日境界をまたぐ場合、経過時間に比例して分配するため小数になり得る。
安静時心拍はAPI提供日付を維持し、睡眠の分析日付はTokyoでの終了日とする。睡眠の元日付は別列に残す。
欠測を0へ変換しない。睡眠時間に段階と短い覚醒を加算せず、AZMへの追加重み付けもしない。

`fitbit_sleep_screen_time`は睡眠開始前2時間のScreen Timeを端末別に返す。
completeと次の開始から終了を推定した区間を窓内で切り詰め、端末内の重複を統合する。
対象区間がない端末は行を作らず、端末横断で合算しない。同一個人の端末を前提とし、複数人のデータを混在させない。
