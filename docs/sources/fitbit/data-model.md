# Fitbitデータモデル

## バッチRawとDB状態

```text
raw/fitbit/v2/<subject_key>/health/batch/<YYYYMMDDTHHMMSSffffffZ>/<sha256>.json.gz
```

通常は7日分の変更を1つのschema version 2 envelopeへまとめる。
subject、バッチの最終取得開始時刻、種別・範囲ごとのschema version 1 Snapshotを持つ。
各Snapshotは取得した半開区間、取得開始時刻、`complete=true`、正規化行、元API pointの全フィールドを保持する。
同じ種別で範囲が重なるSnapshotは同一バッチに入れない。SHA-256はgzip前のenvelopeに対して計算する。
JSONのwire表記自体は保存せず、未知フィールドを含むpoint内容を保持する。gzipは決定的で、Rawはcreate-only。
subjectは安定した疑似識別子を使い、OAuthのIDやtokenをkeyに含めない。

バッチの共通`observed_at`は最も新しいSnapshotの取得開始時刻とし、置換順位は各Snapshot自身の時刻を使う。
既存の`raw/fitbit/v1/<subject>/health/<data-type>/.../*.json.gz`も共通Loader・再構築から読み取れる。
両形式のRawはGCS作成から90日保持する。新しい受付・checkpointファイルはGCSに作らない。
残っている旧受付と旧control JSONにも90日の削除条件を設定する。

`ops.fitbit_daily_state`は利用者ごとの完了日、補完済み同期日、最後に取得した同期時刻、成功した日次実行日を持つ。
`recheck_json`には通常の7日より古い補完範囲、観測した同期時刻、7日後の再確認期限、
未完了cursorと直前の巡回日を保持する。初回取得だけで過去範囲を解消せず、
新しい同期期間を統合しながら日数・時間上限内で再確認する。
`ops.fitbit_batch_intent`はRaw key、取得予定範囲、変更なしで省いた範囲のhash・確認時刻、確定予定の進捗を持つ。
保存予定をDBへ先に確定してからGCSへ保存する。Rawの分析反映は共通Warehouseのtransactionで確定し、
確認時刻・進捗・保存予定の解消を次のDB transactionで確定する。

障害後はまず取込台帳を確認する。反映済みならGCSを読まずに進捗を確定する。
未反映なら対象keyだけを確認し、存在するRawは同じgenerationで読み取る。
Rawが存在しない場合は保存予定の範囲をより新しい時刻で再取得する。
旧`ops.fitbit_raw_intent`も対象keyで復旧し、新しい完全取得が覆う古い保存予定を解消する。
GCSの旧受付自体を新方式の進捗には使わないため、切替前に未完了受付を処理するか、その期間を手動取得する。

## テーブルと単位

Fitbitの初期スキーマは`003_fitbit.sql`、内容hashと旧Raw保存予定は`004_fitbit_acquisition.sql`、
日次進捗とバッチ保存予定は`005_fitbit_daily.sql`で追加する。
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
保持中のRawに元データが含まれる範囲だけ再構築でき、必要ならAPI再取得で補う。

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
