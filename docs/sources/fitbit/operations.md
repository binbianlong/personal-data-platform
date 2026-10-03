# Fitbit運用

## 日次方式

毎朝04:30 Asia/Tokyoの既存Screen Time Reconciliation JobでFitbitを取得する。
GCSリポジトリ、Loader、取込台帳、leaseを共有し、同じMotherDuck接続を再利用する。
この日次方式のクラウド適用、実端末での到達、GCS実操作量・MotherDuck実使用量、通知の発火・復旧は別途確認する。
`enable_fitbit_runtime=false`、有効化時も`fitbit_processing_paused=true`が既定。

## CLI

```bash
pdp fitbit migrate --database /private/path/fitbit.duckdb
pdp fitbit daily
pdp fitbit sync --from 2026-09-20 --to 2026-09-27
pdp fitbit sync --from 2026-09-26T00:00:00+09:00 --to 2026-09-27T00:00:00+09:00 --data-type steps
```

`daily`は未完了保存を復旧して直近7完了日と未取得期間を取得する。`sync`は指定範囲を7日分ずつ手動取得する。
日付指定はTokyo午前0時の半開区間で、`--to`は含まない。日次・睡眠は対応するcivil dateへ補正する。
停止中はAPI・GCS・DBへのアクセスを行わない。手動取得も停止設定に従う。
`migrate --database`を省略するとruntime DB設定を使う。日次Jobの起動時も追記migrationを適用する。

初回・分析定義変更時はdbt Viewを構築する。通常の日次取得後はViewに反映されるため`dbt run`は不要。

```bash
DBT_DUCKDB_PATH=/private/path/fitbit.duckdb dbt run --project-dir dbt --profiles-dir dbt --target local --select 'tag:fitbit tag:screen_time'
DBT_DUCKDB_PATH=/private/path/fitbit.duckdb dbt test --project-dir dbt --profiles-dir dbt --target local --select 'tag:fitbit tag:screen_time'
```

共通Loader・手動監査・再構築は`--source fitbit --stream health`を指定する。
これらは両形式のRawを一覧取得するため、GCS読み取りが発生する。通常の日次JobはFitbitの全Raw監査を実行しない。

## 環境変数

| 変数 | 用途 |
|---|---|
| `PDP_FITBIT_SUBJECT_KEY` | 安定した疑似subject |
| `PDP_FITBIT_OAUTH_CREDENTIALS` | `client_id`・`client_secret`・`refresh_token`を含むOAuth JSON |
| `PDP_FITBIT_OAUTH_CLIENT_ID` / `PDP_FITBIT_OAUTH_CLIENT_SECRET` / `PDP_FITBIT_OAUTH_REFRESH_TOKEN` | JSON未設定時の個別OAuth設定 |
| `PDP_FITBIT_DAILY_ENABLED` | `true`で共通日次JobにFitbitを追加。単独`fitbit daily`には不要 |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`で日次・手動取得を停止 |
| `LOG_LEVEL` | 既定`INFO`。完了監視を使う環境では`INFO`または`DEBUG` |
| `GOOGLE_CLOUD_PROJECT` / `GCS_BUCKET` | 共通GCS設定 |
| `MOTHERDUCK_DATABASE` / `MOTHERDUCK_TOKEN` | 共通DB設定 |

Terraformの`fitbit_secret_ids`には既存OAuth SecretのIDだけを渡す。
推奨形式は`{"PDP_FITBIT_OAUTH_CREDENTIALS":"pdp-fitbit-oauth"}`。
個別OAuth設定を使う場合は3つのIDを渡し、JSON形式と混在させない。
Webhook Authorizationとhealth user IDのSecret参照は不要。
OAuth JSONの秘密値はGit・tfvars・Repository Variables・ログに含めない。

```json
{"client_id":"<client-id>","client_secret":"<client-secret>","refresh_token":"<refresh-token>"}
```

JSONが設定されている場合は個別設定より優先し、不正なJSON・項目欠落・空文字は起動時に拒否する。

## クラウド構成と監視

専用Cloud Run Service、Cloud Tasks queue、タスク用SAを使わず、既存日次JobのSAへOAuth読み取りとRaw作成を許可する。
Loader leaseの競合、日数上限、取得開始から90分の時間上限では`deferred`として次回へ継続する。
APIの1取得20分上限と組み合わせ、共有leaseの期限までに処理を終える余裕を残す。正常終了ではleaseを解放し、異常終了では期限切れ後に再開する。
不明なcommit結果の接続は再利用せず、次回の取込台帳から確認する。

日次結果を`event=fitbit_daily`、`status`、`summary`を持つ1行JSONでstderrへ出す。
CLI結果はstdoutに出す。`summary`にはAPI取得範囲数、Raw保存・省略数、復旧数、圧縮後bytes、最終完全成功からの秒数を含める。
`ops.heartbeat`の`fitbit_daily_pass`は全対象の取得・分析反映・進捗確定を終えた場合だけ更新する。
失敗・延期は成功時刻を進めず、成功履歴がない場合は最初の試行を基準にする。
48時間超の未完了を既存通知先へ報告する。日次の評価なので検出は次回実行まで遅れる場合がある。
Job失敗・実行欠測は共通Reconciliationの監視が検出する。停止中はFitbitの未完了通知を無効にする。

通常の7日取得で変更があればGCSへ1つのgzip Rawを保存し、変更がなければ保存しない。
受付・checkpoint・sidecarは作らず、保存直後のGCS読み直しと一覧取得を省く。
保存対象の増加は90日Lifecycleで抑える。全量の長期Raw複製や毎日の全件監査は行わない。
障害復旧・create-onlyの競合・長期補完・手動監査では追加操作が発生する。
この回数はアプリケーション側の設計値であり、課金操作数や無料枠の適用は実環境の使用量で確認する。

## Webhook方式からの切替

1. 本番と別のbucket・DB・SAで新imageの`fitbit daily`を実行し、5種別の反映、再実行、遅延データ、障害復旧を確認する。
2. 本番DBのmigrationと必要なdbt Viewを準備する。Secret ID設定をOAuthのみの形式へ変更する。
3. 旧購読の対象を確認してGoogle Healthの購読を停止する。旧queue・受付の未完了を処理するか、最古の未完了範囲から切替日までを`fitbit sync`で補完する。新方式は旧受付の本文を自動移行しない。
4. 既存`pdp-fitbit` ServiceがTerraformのstateに存在する場合、旧構成で`deletion_protection=false`を適用してから削除へ進む。
5. 新構成のplanで専用Service・queue・不要SA/IAMの削除、日次Job設定、Raw v1/v2と旧受付・controlのLifecycleを確認する。実際の旧構成が管理外なら別途切替する。
6. 日次Jobへ新imageを適用し、取得を有効にして手動実行と次の定期実行を確認する。停止を解除する場合は`fitbit_processing_paused=false`を設定する。
7. 同じ測定期間のGCS API操作数・object数・圧縮後bytesとMotherDuck使用量を照合し、監視の発火・復旧を確認する。

旧受付・controlは作成から90日で削除対象にする。旧Raw v1は保持中の再生を維持する。
Webhook用Secretの廃止はほかの利用元がないことを確認した後に行う。

## 復旧

日次進捗と未完了保存はMotherDuckで確認する。

```sql
SELECT * FROM ops.fitbit_daily_state;
SELECT raw_key, subject_key, created_at FROM ops.fitbit_batch_intent ORDER BY created_at;
SELECT * FROM ops.fitbit_raw_intent ORDER BY fetched_at;
SELECT * FROM ops.heartbeat WHERE monitor_name LIKE 'fitbit_daily_%';
```

未完了保存があれば次の日次・手動取得で対象keyを確認して再開する。
長い未取得期間は最大90日ずつ補完し、当日まで追いついたときだけ完全成功にする。
通常の再照合より古い修正や初回7日より前の履歴は`fitbit sync`で取得する。
MotherDuck全損時は保持中のRawを共通Rebuildで再生し、Rawに含まれない期間をAPIで補う。
90日を過ぎたRawと、保存を省略した範囲の元Rawについては完全な再構築を保証しない。

## 過去分の一度限りの投入

本番への初回移行時に、一時スクリプトでTakeout ZIPを検証し、MotherDuckへ一度だけ投入する。
このスクリプトはアプリの機能・依存・CLIとして配布しない。スクリプト作成と実投入は未実施。
ZIP由来の健康データ・認証情報・一時スクリプトをGitに追加しない。

一度限りの投入では、次を確認する。

- 元ZIPのhashと処理件数・期間を記録し、元入力と照合結果を手元で保管する。
- 新CSVだけを使い、旧JSONを混ぜず、スマートフォン由来の歩数を除外する。
- 睡眠はAPI reconcileが選択したIDで旧/新アルゴリズムの重複を解消する。v2優先だけで代替しない。
- 同日の安静時心拍の矛盾はAPI照合で確定し、未解決の値は投入しない。
- 部分失敗からの再開と重複防止は一時スクリプト内で管理する。GCS台帳へ架空のオブジェクトを作らない。
- API確定済み期間を古いZIPで上書きしない。継続取り込みとの境界を重ねて照合する。

### 2026-09-27時点の調査記録

調査対象は`/Users/binbi/Downloads/takeout-20260926T121107Z-1-001.zip`、
SHA-256は`ea328ec61e714766208e1cfea368c010c25d28fca48668f9d49676ef8be1f303`。
調査記録では心拍9,029,170行、歩数49,564行（端末由来47,294行）、安静時心拍254行/252日、
AZM 2,271行、睡眠611 ID。安静時心拍は2日分に矛盾がある。
過去のAPI照合では有効な睡眠341 IDがZIPと一致した（v2 259件、v1 82件）。投入時には改めて照合する。
ローカルDBサイズはMotherDuck実ストレージや課金量の証明として扱わない。
