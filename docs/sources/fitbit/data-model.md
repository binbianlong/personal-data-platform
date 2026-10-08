# Fitbitデータモデル

## APIからの直接取込

全ページ取得を検証し、1日・1種別の正規化データとcoverageを同じtransactionへ書き込む。
APIレスポンスと未知フィールドは処理中のメモリだけで扱い、Rawとして永続保存しない。
`fetched_at`は全ページ取得の開始時刻。`source_key`は`fitbit-api:<UUID>`形式の取得識別子で、保存先を表さない。
以前の取込に付いたGCS keyは過去の識別子として残す。既存の正規化データとスキーマは保持する。
`ops.fitbit_coverage`には現在の取得範囲・時刻・比較用hashを残し、Raw取込台帳への新規登録は行わない。
通知・attempt・cursorの永続台帳や同期checkpointは持たず、復旧には期間指定のAPI再取得を使う。

## テーブルと単位

| base table | 保存する意味 |
|---|---|
| `fitbit_activity_interval` | `metric`で歩数とAZMを区別。開始・終了、歩数または重み付け済み分、zone |
| `fitbit_resting_heart_rate_daily` | `source_date`と`beats_per_minute`。提供元の日付をDATEで保存 |
| `fitbit_sleep_session` | 開始・終了、API summaryの`sleep_minutes`、種別、main sleep区分、提供元の日付 |
| `fitbit_sleep_detail` | `kind`で段階と短い覚醒を区別。`sleep_id`、親の日付、区間、category |
| `fitbit_heart_rate_minute` | UTC分ごとの平均・最小・最大bpm、未知のsample countはNULL |

全テーブルはsubject・取得時刻・入力key・取込時刻を保持する。
区間には元UTCオフセットを残す。取得元はGoogle wearables APIに統一し、origin列は持たない。
IDのないサンプルはsubject・種別・時刻/区間から決定的なIDを作る。
歩数/AZMの主キーは`(subject_key, metric, record_id)`、睡眠詳細は`(subject_key, kind, record_id)`、
日次心拍と睡眠セッションは`(subject_key, record_id)`。metricやkindの異なる同じIDは別の記録として保持する。

安静時心拍と睡眠はproviderのDATEで取得範囲を判定する。
日付を比較するときだけUTC午前0時に変換し、物理時刻の開始として保存しない。
睡眠詳細の提供元日付は親と一致し、物理区間は親の範囲内でなければならない。
段階と短い覚醒は重なりを保持し、秒数は開始・終了から算出する。
睡眠分数はAPI summaryを使い、セッションの経過分数で代用しない。

分心拍は`average`・`minimum`・`maximum`とUTC分の開始/終了を持ち、主キーは
`(subject_key, data_source_family, start_at, aggregation_version)`である。
`sample_count`はNULLのままとする。

## 範囲置換と再実行

全ページ取得済みのcursor範囲だけを置き換え、空結果も反映済み範囲として残す。
`ops.fitbit_coverage`が全種別の取得順位と現在内容のhashを保持する。
subject・種別・data source family・aggregation versionごとに範囲を管理する。
通常記録は`records-v1`、分心拍は`heart-rate-minute-v1`で区別する。
順位は`(fetched_at, source_key)`で、新しい範囲を保護し、古い取得は未反映部分だけを更新する。

保存済みRawを先に再生する。取得範囲が完全に覆われ、未知フィールドを含む内容hashが一致する場合だけ新しいRawを省く。
比較用hashはSnapshotから`fetched_at`だけを除き、正規化行と元API pointの並びを揃える。Rawは取得時の並びを維持する。
範囲の重なりや記録IDの移動などで判定が不確かならフルRawを保存する。`A→B→A`では3回保存し、空の完全取得も同じ規則を使う。

同一範囲が現在と同じ内容なら分析行の書き換えを省いて取得順位を進め、A→B→Aも反映する。
睡眠IDの日付移動は子も親ID単位で置換する。`ops.fitbit_deleted_record`に削除済み主レコードIDの順位を残し、
古い取得による復活を防ぐ。より新しい取得による復帰は許可する。密なサンプルは一括SQLで書く。

取込結果は`ops.ingestion_metadata`へ分析行と同じtransactionで保存する。失敗時の処理は[共通永続化契約](../../platform/architecture.md#loaderと永続化)に従う。
Raw省略期間は元Rawの期限切れ後にRawだけの完全再構築を保証できないため、必要ならAPI再取得で補う。

## 分析ビュー

| marts view | 内容 |
|---|---|
| `daily_fitbit_health` | 歩数・分心拍の日次指標・安静時心拍・AZM・睡眠の日次集計 |
| `daily_fitbit_heart_rate_minute` | 分平均の平均、分最小値の最小、分最大値の最大、観測分数の日次集計 |
| `fitbit_steps_time_series` | 歩数区間 |
| `fitbit_heart_rate_minute_time_series` | 分心拍の平均・最小・最大 |
| `fitbit_sleep_sessions` | 睡眠セッションとsummary |
| `fitbit_sleep_screen_time` | 睡眠開始前2時間の端末別Screen Time |

日付比較はAsia/Tokyo。歩数とAZMが日境界をまたぐ場合、区間の経過時間に比例して分配するため小数になり得る。
日次安静時心拍はAPI提供日付を維持する。睡眠の分析日付はTokyoでの終了日、元日付は別列に残す。
欠測を0へ変換しない。睡眠時間にはsession summaryを使い、段階と短い覚醒を加算しない。
AZMへの追加重み付けもしない。

Screen Timeはcompleteと次の開始から終了を推定した区間を利用し、2時間の窓で切り詰めて端末内の重複を統合する。
MacとiPhoneは別行で、同時使用を端末横断で合算しない。対象区間がない端末は行を作らない。
現行は同一個人の端末を前提とし、複数人のデータを混在させない。
