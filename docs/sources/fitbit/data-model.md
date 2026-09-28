# Fitbitデータモデル

## API Rawと受付記録

```text
raw/fitbit/v1/<subject_key>/health/<data-type>/<YYYYMMDDTHHMMSSffffffZ>/<sha256>.json.gz
receipts/fitbit/v1/<YYYY-MM-DD>/<受付ID>.json
raw/fitbit/v1/_control/device-sync/<subject_key>.json
```

subjectは個人を表す安定した疑似識別子を使う。OAuthのIDやtokenをkeyに含めない。
Rawはschema version 1のJSON envelopeで、subject、data type、取得した半開区間、取得開始時刻、
`complete=true`、正規化行、元API data pointの全フィールドを保持する。SHA-256はgzip前のenvelopeに対して計算する。
JSONのwire表記自体は保存せず、未知フィールドを含むpoint内容を保持する。gzipは決定的で、Rawはcreate-only。
Rawは90日保持する。端末同期checkpointは世代条件付きで更新する小さなcontrol JSONで、Rawの削除対象から外す。
共通`observed_at`にはこのsourceでは取得開始時刻を使い、遅れて完了した古い取得による上書きを防ぐ。

受付記録は取得予定区間、保存済みRawのkey/generation、区間ごとの完了状態を持つ。
変更なしで保存を省いた区間は取得時刻・内容hashと完了を記録し、旧形式の受付も読み取る。
generationの条件付き更新を使い、全通知履歴を毎回複製しない。Raw保存予定はDBに先に確定する。
受付とRawの作成は別操作なので、Raw保存後・参照保存前の障害では未参照Rawが残り得る。
再試行時は保存予定、GCS、取込台帳を照合し、存在しない予定Rawは元の拡張範囲をより新しい時刻で再取得する。
Raw参照を保存済みなら同じgenerationを使う。

## テーブルと単位

Fitbitの初期スキーマは`003_fitbit.sql`、内容hashとRaw保存予定の拡張は追記migrationとする。
適用済みmigrationは変更しない。

| base table | 行とvalueの単位 |
|---|---|
| `fitbit_steps` | 歩数区間・歩数 |
| `fitbit_heart_rate` | 心拍サンプル・bpm |
| `fitbit_resting_heart_rate` | 提供元の日付ごとの安静時心拍・bpm |
| `fitbit_active_zone` | アクティブゾーン区間・既に重み付けされた分 |
| `fitbit_sleep` | 選択済み睡眠セッション・summaryの睡眠分 |
| `fitbit_sleep_stage` | 親睡眠に含まれる段階区間・秒 |
| `fitbit_sleep_wake` | 段階と重なる短い覚醒等の区間・秒 |

共通列はsubject、record ID、置換cursor、UTC開始/終了、value、元UTCオフセット、提供元日付、親ID、
category、main sleep区分、origin、取得時刻、入力key、取込時刻。主キーは`(subject_key, record_id)`。
IDのないサンプルはsubject・種別・時刻/区間から決定的なIDを作る。
日付cursorはcivil dateをUTC午前0時で表した比較用の値であり、実時刻ではない。
睡眠明細のcursorは親と一致し、物理区間は親の範囲内でなければならない。

## 範囲置換と再実行

全ページ取得済みのcursor範囲だけを置き換える。空結果も反映済み範囲として残す。
`ops.fitbit_coverage`は重ならない範囲ごとの取得順位と現在内容のhashを保持する。
順位は`(fetched_at, source_key)`。新しい範囲を保護し、古い取得は未反映部分だけを更新する。

Snapshot全体から`fetched_at`だけを除き、未知の元API項目も含めた別のhashを重複判定に使う。
ページ順の差で保存が増えないよう、比較用hashでは正規化行と元API pointの並びだけを揃える。
Rawには取得時の並びをそのまま残す。
同じ取得範囲が完全に覆われ、hashが一致し、同じ利用者・種別に未解決のRaw保存予定がない場合だけ
新しいRawを省く。範囲の重なりや記録IDの移動など、判定に不確実さがあればフルRawを保存する。
`A→B→A`では3回保存する。空の完全取得も同じ規則で照合する。

同一範囲で現在と同じ内容なら分析行の書き換えを省き、取得順位を進める。
過去のhashを永久に除外しないためA→B→Aも反映する。
同じ睡眠IDの日付が動いた場合は子も親ID単位で置き換える。
`ops.fitbit_deleted_record`に削除済み主レコードIDの順位を残し、移動後に削除されたIDを古い取得が復活させない。
より新しい取得による復帰は許可する。密なサンプルの書込は一括SQLとし、1行ずつDBと往復しない。

取引境界は共通Warehouseが所有する。Rawの取込結果を`ops.ingestion_metadata`へ記録し、
分析行と台帳を同じtransactionでcommitする。
commit結果不明・rollback失敗では接続を破棄し、再接続後の台帳で判定する。
Rawを省いた期間は元のRawが90日後に消えるとRawだけでの完全再構築を保証しない。
これはScreen Timeと同じ復旧範囲であり、必要ならAPI再取得で補う。

## 分析ビュー

| marts view | 内容 |
|---|---|
| `daily_fitbit_health` | 歩数・心拍平均/最小/最大/件数・安静時心拍・AZM・睡眠の日時集計 |
| `fitbit_steps_time_series` | 歩数区間 |
| `fitbit_heart_rate_time_series` | 心拍サンプル |
| `fitbit_sleep_sessions` | 睡眠セッションとsummary |
| `fitbit_sleep_screen_time` | 睡眠開始前2時間の端末別Screen Time |

日付比較はAsia/Tokyo。歩数とAZMが日境界をまたぐ場合、区間の経過時間に比例して分配するため小数になり得る。
日次安静時心拍はAPI提供日付を維持する。睡眠の分析日付はTokyoでの終了日、元日付は別列に残す。
欠測を0へ変換しない。睡眠時間にはsession summaryを使い、段階と短い覚醒を加算しない。
AZMへの追加重み付けもしない。

Screen Timeはcompleteと次の開始から終了を推定した区間を利用し、2時間の窓で切り詰めて端末内の重複を統合する。
MacとiPhoneは別行で、同時使用を端末横断で合算しない。対象区間がない端末は行を作らない。
現行は同一個人の端末を前提とし、複数人のデータを混在させない。
すべてViewのため通常取り込み後の`dbt run`は不要。初回・定義変更時に構築し、既存のread-only MCPから参照する。
