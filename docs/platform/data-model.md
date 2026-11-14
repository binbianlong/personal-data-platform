# データモデル

`base`は提供元の記録と本人が登録した固定情報を保存する18の物理表で構成する。`ops`には排他leaseとschema migration台帳だけを保存する。`marts`はdbtによる分析ビューの領域で、baseline migrationではビューを作らない。

## 時刻・欠測・識別子

- 時刻は`TIMESTAMPTZ`で保存し、小数秒を保持する。分析の日次境界は原則`Asia/Tokyo`とする。
- `source_date`は提供元が付けた日付をそのまま保持する。UTCオフセットは提供元の秒数を別列に残す。
- `NULL`は不明・未取得・未算出を表す。欠測を0やFALSEで埋めない。未知の分類値も保持する。
- 記録の成立に必要な時刻・端末・指標値・親IDは`NOT NULL`とする。区間の終了は開始より後、心拍はUTC分境界かつ有限な`0 ≤ min ≤ mean ≤ max`、測位の緯度経度は有効範囲内とする。歩数・AZM・提供された睡眠分数・安静時心拍は有限非負を保つ。提供元の日付、オフセット、任意の詳細値はNULLを許容する。
- `device_key`は各ソース内の端末キーで、共通の端末対応表を持たない。
- 各表の先頭のID列を主キーとする。`health_daily`は`source_date`、`app`は`(platform, bundle_id)`を主キーとする。
- ソースのaccount・取り込み時刻・Rawファイル管理列は`base`に保存しない。

全表・全列の意味、単位、時刻とNULLの扱いはSQLの`COMMENT`からDuckDBカタログでも参照できる。

## base

| 表 | 1行の粒度 | 値の扱い |
|---|---|---|
| `sleep_session` | 提供元の睡眠セッション | `sleep_minutes`は提供元の実睡眠分数。開始・終了差で補完しない。日次分析は日本時間の終了日へ帰属させる。 |
| `sleep_detail` | 睡眠段階・短い覚醒の区間 | `sleep_session_id`で親睡眠へ対応する。`kind`をまたぐ重複区間を実睡眠分数として足さない。 |
| `heart_rate_minute` | 系列・UTC開始分・集計方式 | 平均・最小・最大心拍はbpm。終了時刻は開始の1分後。未観測分やサンプル数は作らない。 |
| `steps_interval` | 提供元の歩数区間 | `steps`は区間内の増分。元の区間長と記録された0歩を保持する。 |
| `active_zone_interval` | 提供元のAZM区間 | `active_zone_minutes`は重み付け済み。cardio・peakを再度2倍しない。 |
| `exercise_session` | 提供元の運動セッション | `active_seconds`は休止を除く秒数。未提供時に経過時間で埋めない。 |
| `sedentary_interval` | 提供元の不活動区間 | 非装着や欠測を不活動として記録しない。 |
| `distance_interval` | 提供元の距離区間 | 距離はm。位置情報からの独自推定距離と単純に足さない。 |
| `heart_rate_zone_interval` | 提供元の心拍ゾーン区間 | 分類と元の時刻を保持する。AZMとは別の量として扱う。 |
| `health_daily` | 提供元の日付 | 日次心拍、HRV、呼吸数、SpO2、皮膚温、VO2 maxを横持ちする。取得した指標だけを更新し、正常な空の応答では当該指標をNULLにする。 |
| `hrv_sample` | 提供元のHRVサンプル | 測定時刻とRMSSD・SDNNのms値を保持する。終了時刻や睡眠IDを推測しない。 |
| `sleep_respiratory_summary` | 提供元の睡眠呼吸サンプル | 同日内の複数サンプルを保持する。全睡眠・各段階の呼吸数、標準偏差、SNRを区別する。 |
| `location_point` | 端末と内容で識別した測位記録 | 緯度経度は度、精度はm、速度はkm/h。活動ラベル配列のNULL・空配列・未知値を保持する。 |
| `iphone_screen_time_event` | iPhoneのアプリ開始・終了イベント | `event_key`で再送を冪等に扱う。`state`は`start`または`end`、`is_valid`は分析への採用可否。 |
| `mac_screen_time_event` | Macのアプリ開始・終了イベント | iPhoneと独立したイベント履歴。送信ファイルの削除や消失だけでは無効化しない。 |
| `app` | OSとbundle ID | `platform`は`ios`または`macos`。手動で表示名を登録する。未登録でもイベントを保存できる。 |
| `profile` | 本人の現在の設定 | 性別、身長cm、生年月日、自宅・大学の場所ID。年齢や過去の住所履歴を保存しない。 |
| `place` | 本人が登録した場所 | 表示名、緯度経度、判定半径m。実値を推測して補完しない。 |

`health_daily`のSpO2上下限は信頼区間であり最小・最大ではない。SpO2標準偏差は提供元の過去7〜30日の日次平均に関する値である。VO2 maxの推定フラグや睡眠段階など、提供元の推定値は提供元の記録として保持する。

`sleep_detail.sleep_session_id`、`profile.home_place_id`、`profile.university_place_id`は論理的な参照である。DuckDBでは同一トランザクション内の親子の削除・再登録に外部キーの制約があるため、物理FKは付けず、書き込み処理とデータ検証で対応の整合性を確認する。

## ops

| 表 | 列 | 用途 |
|---|---|---|
| `job_lock` | `job_name`, `owner_id`, `expires_at` | 期限付きの共有writer lease。所有者と失効時刻を保持する。 |
| `schema_migration` | `migration_id`, `checksum`, `applied_at` | 適用済みSQLファイルとSHA-256、適用時刻。変更済み・未知の履歴は拒否する。 |

実行結果や失敗段階はJSONログへ記録する。

## marts

分析の公開先は`iphone_screen_time_interval`、`mac_screen_time_interval`、`daily_steps`、`daily_active_zone`、`daily_iphone_screen_time_total`、`daily_mac_screen_time_total`、`daily_sleep`、`daily_sleep_detail`、`daily_life`の9ビューである。dbtがビュー定義と更新を管理する。Screen Timeの区間化と日次集計、日またぎ区間の按分、睡眠詳細の重複除去などの計算をここで行う。

## 初期化と履歴保護

`pdp migrate --database /path/to/local.duckdb`は空のローカルDBへbaselineを適用する。適用済みSQLのchecksumを検証し、同じDBで繰り返しても既存データを変更しない。SQL・データ・適用台帳の更新はトランザクションで確定する。COMMITの成否が不明な接続は再利用しない。

旧スキーマのDBへこのbaselineを直接適用しない。履歴のない既存`base`・`ops`・`marts`のrelationや、未知・変更済みのmigration履歴があるDBは拒否し、空の移行先を必要とする。
