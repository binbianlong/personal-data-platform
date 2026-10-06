# Fitbitデータモデル

## API Raw

```text
raw/fitbit/v3/<subject_key>/health/<YYYYMMDDTHHMMSSffffffZ>/<bundle_id>/<object_index>/<sha256>.json.gz
```

Raw v3は完全取得単位の直接gzip JSON配列で、gzip後最大16 MiB。一つの取得を分割せず、各objectを単独で再生できる。
元APIの未知フィールド、取得範囲・時刻、正規化結果を保持する。subjectは安定した疑似識別子で、OAuth IDやtokenをkeyへ含めない。
`fetched_at`は全ページ取得の開始時刻、objectの`observed_at`は含まれる取得単位の最大`fetched_at`。
`bundle_id`と0始まりの`object_index`がobjectを識別し、`sha256`はgzip前のJSON全体から計算する。
通知・attempt・bundle・cursorの永続台帳、GCS receipt、同期checkpointは持たない。
決定的gzip、create-only保存、90日保持、generation固定の再生は[共通Raw契約](../../platform/architecture.md)に従う。

## テーブルと単位

| base table | 行とvalueの単位 |
|---|---|
| `fitbit_steps` | 歩数区間・歩数 |
| `fitbit_heart_rate_minute` | UTC分ごとの平均・最小・最大bpm、sample countはNULL |
| `fitbit_resting_heart_rate` | 提供元の日付ごとの安静時心拍・bpm |
| `fitbit_active_zone` | アクティブゾーン区間・既に重み付けされた分 |
| `fitbit_sleep` | 選択済み睡眠セッション・summaryの睡眠分 |
| `fitbit_sleep_stage` | 親睡眠に含まれる段階区間・秒 |
| `fitbit_sleep_wake` | 段階と重なる短い覚醒等の区間・秒 |

分心拍以外の共通列はsubject、record ID、置換cursor、UTC開始/終了、value、元UTCオフセット、提供元日付、親ID、
category、main sleep区分、origin、取得時刻、入力key、取込時刻。主キーは`(subject_key, record_id)`。
IDのないサンプルはsubject・種別・時刻/区間から決定的なIDを作る。
日付cursorはcivil dateをUTC午前0時で表した比較用の値であり、実時刻ではない。
睡眠明細のcursorは親と一致し、物理区間は親の範囲内でなければならない。

分心拍は`average`・`minimum`・`maximum`とUTC分の開始/終了を持ち、主キーは
`(subject_key, data_source_family, start_at, aggregation_version)`である。
取得順位・入力key・取込時刻を保持し、`sample_count`はNULLのままとする。

## 範囲置換と再実行

全ページ取得済みのcursor範囲だけを置き換え、空結果も反映済み範囲として残す。
`ops.fitbit_coverage`、分心拍の`ops.fitbit_minute_coverage`が重ならない範囲の取得順位と現在内容のhashを保持する。
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
| `fitbit_heart_rate_time_series` | 分心拍時系列と同じ列を公開する互換View |
| `fitbit_sleep_sessions` | 睡眠セッションとsummary |
| `fitbit_sleep_screen_time` | 睡眠開始前2時間の端末別Screen Time |

日付比較はAsia/Tokyo。歩数とAZMが日境界をまたぐ場合、区間の経過時間に比例して分配するため小数になり得る。
日次安静時心拍はAPI提供日付を維持する。睡眠の分析日付はTokyoでの終了日、元日付は別列に残す。
欠測を0へ変換しない。睡眠時間にはsession summaryを使い、段階と短い覚醒を加算しない。
AZMへの追加重み付けもしない。

Screen Timeはcompleteと次の開始から終了を推定した区間を利用し、2時間の窓で切り詰めて端末内の重複を統合する。
MacとiPhoneは別行で、同時使用を端末横断で合算しない。対象区間がない端末は行を作らない。
現行は同一個人の端末を前提とし、複数人のデータを混在させない。
