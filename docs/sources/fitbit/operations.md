# Fitbit運用

## 現在の状態

2026-09-30時点で取得・保存の見直し、補修競合の回復、JSONログと完了欠測監視のローカル実装・オフライン検証を完了した。
この変更に対するCI、隔離環境のGoogle Health実端末・権限、GCS実操作量、MotherDuck実CU使用量は未検証。
クラウド適用、PDP購読登録、本番ZIP投入、実端末から分析までの到達も未検証。Terraformは`enable_fitbit_runtime=false`、
有効化時も`fitbit_processing_paused=true`が既定。

検証記録:

- 2026-09-27: Ruff・strict mypy・pytest 505件、Terraform fmt/validate/mock test 11件、wheelビルドを確認した。
- 2026-09-28: Ruff・整形・strict mypy・pytest 511件の成功を確認した。
- 2026-09-28: 取得・保存の見直し後にRuff・strict mypy・pytest 537件、Terraform fmt/validate/mock test 11件を確認した。
- 2026-09-30: 受付の限定再読込、補修の延期・失敗、起動時のJSONログをPython 3.13で検証した。
  Terraformの3ルートでfmt/validate/mock test計14件とwheelビルドを確認した。通知の発火・復旧は未検証。
- コンテナ実行は未確認。2026-09-27の検証時はDocker Engineが起動していなかった。

CI、クラウド動作、実CUはこのローカル検証の対象外。

## CLI

追加スキーマを明示的に適用し、dbt Viewを構築する。ローカルDBでの準備例:

```bash
pdp fitbit migrate --database /private/path/fitbit.duckdb
DBT_DUCKDB_PATH=/private/path/fitbit.duckdb dbt run --project-dir dbt --profiles-dir dbt --target local --select 'tag:fitbit tag:screen_time'
DBT_DUCKDB_PATH=/private/path/fitbit.duckdb dbt test --project-dir dbt --profiles-dir dbt --target local --select 'tag:fitbit tag:screen_time'
```

`--database`省略時はruntime DB設定を使うため、実行前に接続先を確認する。
有効化済み環境の手動取得・復旧とHTTPサービス起動:

```bash
pdp fitbit sync --from 2026-09-20 --to 2026-09-27
pdp fitbit sync --from 2026-09-26T00:00:00+09:00 --to 2026-09-27T00:00:00+09:00 --data-type steps
pdp fitbit repair
pdp fitbit serve
```

syncの日付指定はTokyo午前0時の半開区間。`--to`は含まない。
日次・睡眠は対応するcivil dateへ補正する。syncはAPI・GCS・DBを操作する。
serveはHTTPサービスを起動する。repairは有効フラグがfalseなら何もしない。
共通Loader・監査・再構築では`--source fitbit --stream health`を指定する。

ZIP取り込み・照合用CLIやファイル取り込み台帳はアプリに持たない。
過去分の投入は[過去分の一度限りの投入](#過去分の一度限りの投入)に従う。

## 環境変数

| 変数 | 用途 |
|---|---|
| `PDP_FITBIT_SUBJECT_KEY` | 安定した疑似subject |
| `PDP_FITBIT_OAUTH_CREDENTIALS` | API OAuthのJSON。`client_id`・`client_secret`・`refresh_token`を1つのSecret versionで渡す |
| `PDP_FITBIT_OAUTH_CLIENT_ID` / `PDP_FITBIT_OAUTH_CLIENT_SECRET` / `PDP_FITBIT_OAUTH_REFRESH_TOKEN` | 移行中の個別OAuth設定。JSON設定がない場合だけ使用 |
| `PDP_FITBIT_HEALTH_USER_ID` | Webhookの対象owner。OAuthのownerと一致させる |
| `PDP_FITBIT_WEBHOOK_AUTHORIZATION` | 購読に設定するAuthorization共有値 |
| `PDP_FITBIT_SERVICE_URL` | ServiceのHTTPS URL。内部タスクOIDC audienceにも使う |
| `PDP_FITBIT_TASK_SERVICE_ACCOUNT` | タスク呼出用SA email |
| `PDP_FITBIT_TASKS_PARENT` | `projects/.../locations/.../queues/pdp-fitbit` |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`でworkerのAPI/DB更新、補修のAPI照会と定期受付作成を停止 |
| `PDP_FITBIT_REPAIR_ENABLED` | `true`で受付再投入、端末同期補完、週次照合を有効化 |
| `LOG_LEVEL` | Fitbitアプリlogの重要度。既定は`INFO`。完了監視を利用する環境では`INFO`または`DEBUG`を使用 |
| `GOOGLE_CLOUD_PROJECT` / `GCS_BUCKET` | 既存GCS設定 |
| `MOTHERDUCK_DATABASE` / `MOTHERDUCK_TOKEN` | 既存DB設定 |

TerraformにはOAuth JSON・health user ID・Webhook Authorizationの3つの既存Secret Manager IDを
`fitbit_secret_ids`で渡す。移行中は従来の5つのIDも使用できるが、両形式を混在させない。
秘密値・秘密versionの本文をstateに持たせない。
Reconciliation JobにはOAuth JSONとhealth user IDを参照させ、Webhook Authorizationは渡さない。
MotherDuck secretは既存参照を使う。旧環境リポジトリへの実行時依存はない。

### OAuth Secretの集約

OAuth JSONの形式は次のとおり。値はprivateなファイルまたは標準入力からSecret Managerへ登録し、
Git、tfvars、Repository Variables、ログには含めない。

```json
{"client_id":"<client-id>","client_secret":"<client-secret>","refresh_token":"<refresh-token>"}
```

JSONが設定されている場合は3項目を同じversionから読み、個別設定より優先する。
不正なJSON、項目の欠落、空文字、文字列以外の値は起動時に拒否し、個別設定へは切り戻さない。

1. 個別設定を残したまま、JSONに対応したimageをServiceとreconciliation Jobへdeployする。
2. 現行の3値をまとめた`pdp-fitbit-oauth` Secretを作成し、1つのversionを登録する。
3. `fitbit_secret_ids`とRepository Variable `PDP_FITBIT_SECRET_IDS`を次の形式に変更し、planを確認する。
   `PDP_FITBIT_HEALTH_USER_ID`とWebhookのIDは現行のものを引き継ぐ。

   ```json
   {"PDP_FITBIT_OAUTH_CREDENTIALS":"pdp-fitbit-oauth","PDP_FITBIT_HEALTH_USER_ID":"pdp-fitbit-health-user-id","PDP_FITBIT_WEBHOOK_AUTHORIZATION":"pdp-fitbit-webhook-authorization"}
   ```

4. apply後に両実行主体のSecret読み出し、OAuth更新とGoogle Health APIの読み取り、Webhook認証を確認する。
   `fitbit_processing_paused`と既存Schedulerの停止状態は維持する。
5. 旧3Secretを参照する稼働revision、Job template、実行中Job、CI設定やほかの利用元がないことを確認する。
   rollbackに必要な値をprivateにバックアップしてから旧versionをdestroyする。
   古いimageへ戻す場合は個別Secretのversionと参照設定も復元する。
6. project内の有効versionが7から5になったことと、同じ請求アカウントの合計を確認する。

EnabledとDisabledのversionはともに課金対象なので、disableだけでは集約が完了しない。
MotherDuck本番・preflight、health user ID、WebhookのSecretは分離を維持する。
無料枠は請求アカウント全体で有効6版と月10,000回のアクセス。
version更新後は新旧versionの併存数を確認し、Secret環境変数を再読み込みするServiceの新revisionを作成する。
旧versionのdestroyは全利用元の切替確認後に行う。[Secret Manager料金](https://cloud.google.com/secret-manager/pricing)

## クラウド構成と定期補修

`enable_fitbit_runtime=true`で専用Service・queue・SA・IAM・Raw lifecycleを追加する。
Cloud Runはmin 0 / max 1、HTTP同時処理16。Cloud Tasksは同時dispatch 1、最大20回/24時間の再試行。
内部workerも既存Screen Timeと同じ`loader` leaseを取得する。ロック競合時は503で再試行する。
ロック競合や受付の読み取り競合による再試行はINFOの延期logに記録し、実際の処理失敗と区別する。
異常終了で残ったleaseは期限切れ後に回復する。DB確定結果が不明な接続を再利用しない。

既存のScreen Time reconciliation JobにFitbit補修を接続し、毎朝04:30 Asia/Tokyoに1回実行する。
未完了受付を再投入する。`pairedDevices.list`の全ページを調べ、機種名に依存せず最新の同期時刻を持つ
`TRACKER`を選ぶ。初回はTokyoの直近7完了日を取得し、その後は前回成功した同期日から新しい同期日まで
7日超の空白も補完する。1回に作る受付は最大90日分とし、すべて完了してからcheckpointを進める。
`lastSyncTime`の進展はAPI照合の契機であり、データ到着の保証ではない。週次照合が遅れて到着したデータを確認する。
週1回は直近7完了日を5種別すべて照合し、同じ週の受付を再利用する。初回取得と同週の重複照合は省く。
通常通知に意図的な待ち時間を設けない。
未取込Raw、分析ビューの存在/照会、GCS保持期限を共有reconciliationで監査する。

API Rawと受付記録はGCS作成から90日で削除対象。Rawが93日以降も残る場合は監査失敗とする。
この3日は非同期削除の監査猶予で、93日保持を保証しない。端末同期checkpointのJSONは削除対象外。
未完了受付が87日以上なら保持期限が近い異常として報告する。
受付の最新時刻・完了時刻・未完了数・最古経過時間を補修logに出し、端末からの通知がないことだけでは障害扱いしない。
受付の読み取り中に更新があった場合は最新の世代を取得し直す。本文の読み取りは合計3回までとし、
一覧取得で上限に達した受付は延期件数に数えて、ほかの受付の確認を続ける。完了済み受付の本文は取得しない。
権限不足、通信障害、不正な受付内容は更新競合として省略しない。
`repair`の結果JSONには`status`、`deferred_phases`、`receipt_read_deferred_count`、
`full_success_age_seconds`、`last_full_success_at`を含める。
状態は`disabled`・`paused`・`succeeded`・`deferred`・`failed`。ロックや受付読み取りの競合だけなら
終了コード0で次回へ延期する。再投入失敗、監査失敗、保持期限リスクは延期より優先し、終了コード1となる。
延期段階は`scheduled_receipts`・`receipt_inventory`・`raw_audit`・`orphan_recovery`で記録する。
定期受付の読み取りを延期した場合も、完了checkpointを進めずに既存受付の再投入を続ける。
`succeeded`は今回の補修巡回を終えた状態であり、非同期workerの全受付完了は意味しない。
端末一覧・reconcileのHTTP照会試行数、成功したSnapshot取得件数、変更なしで省いたRaw件数、
新規Raw件数と圧縮後bytesを別々のlogに記録する。実際のGCS操作数・増加量は隔離環境で照合する。
`serve`・`sync`・`repair`は起動時に共通のlog設定を適用する。Fitbitアプリlogは`severity`と`message`を
持つ1行JSONでstderrへ出力し、CLIの結果JSONはstdoutに出力する。既存のroot loggerへ重複出力しない。
Cloud RunはJSONの`severity`を重要度として認識する。[Cloud Runのログ仕様](https://docs.cloud.google.com/run/docs/logging)
Service処理失敗のlog alertは`jsonPayload.message`と移行中の旧`textPayload`の両方を検知する。

補修の最終結果は`event="fitbit_repair"`と`status`付きで記録する。処理中の例外も`status="failed"`で
ERROR logに残し、元の失敗を呼出元へ伝える。
`status="succeeded"`だけを既存reconciliation Jobの成功メトリクスに数える。
既存の`ops.heartbeat`に、初回補修の監視開始時刻（`fitbit_repair_started`）と完全な巡回の最終成功時刻
（`fitbit_repair_pass`）を別々に保存する。初回の開始時刻は1回だけ保存し、並行実行で置き換えない。
延期・失敗時は最終成功を進めず、成功が一度もなければ初回開始からの経過時間を報告する。
この経過秒数を現在のlogからdistribution metricへ抽出し、60秒の集計で平均値が172800秒を超えた場合に
既存メール通知先へ送る。実行IDなどの個別ラベルは付けず、成功時の0秒の測定で回復する。
測定は日次処理時に行うため、最終成功から48〜72時間程度に処理時間を加えて検知する。
Jobが起動せず測定自体がない場合は、共有Reconciliationの48時間の完了数監視で検出する。
`enable_fitbit_runtime=false`では監視リソースを作らず、`fitbit_processing_paused=true`では
補修の経過時間通知ポリシーを無効にしてメトリクスの履歴を維持する。既存のreconciliation Job監視も継続する。
`LOG_LEVEL=WARNING`以上では成功・延期の経過時間logが出ず補修の監視ができないため、監視環境では使用しない。

古い成功logだけでは新しい経過時間metricを検証できない。有効化・再開・監視設定の変更後は、次の確認を行う。

1. 稼働環境の`LOG_LEVEL`が`INFO`または`DEBUG`であることを確認する。
2. 定期または手動で既存reconciliation Jobを実行し、`event="fitbit_repair"`、`status="succeeded"`のlogを確認する。
   手元での`pdp fitbit repair`はCloud Run Jobのリソースラベルがないため、監視用データ点の代用にしない。
3. Cloud Monitoringで`logging.googleapis.com/user/pdp-fitbit-repair-success`と
   `logging.googleapis.com/user/pdp-fitbit-repair-success-age`に新しいデータ点があることを確認する。
   完了logと0秒の測定値の両方を確認するまで補修監視の準備完了としない。
4. 隔離環境で補修が完了しない場合の発火と、正常な完了後の復旧・通知先への到達を確認する。

最初のデータ点と設定変更後の再確認については[欠測監視の仕様](https://docs.cloud.google.com/monitoring/alerts/metric-absence)を参照する。

## 復旧と停止

| 状態 | 復旧 |
|---|---|
| queue登録失敗・再試行終了 | 残った受付を定期repairで再投入 |
| Raw保存前に処理停止 | DBの保存予定とGCS・取込台帳を照合。Rawがなければ元の拡張範囲をより新しい時刻で再取得 |
| Raw保存後に処理停止 | 保存済み参照を使って同じgenerationを再試行。未参照Rawは定期監査で取り込む |
| DB commit結果不明 | 接続を破棄。新接続で取込台帳を確認し、成功済みなら再書込を省略 |
| OAuth失効・権限不足 | secret/owner/権限を確認し、復旧後に受付を再投入 |
| API制限・一時障害 | Cloud Tasksのbackoffで再試行。途中結果は保存しない |
| 7日超の端末同期空白 | 再開後の端末同期補修で前回成功日から自動補完。対象は1回最大90日分ずつ発行 |
| 端末同期以外の古い修正 | `sync --from ... --to ...`で指定期間を補完 |
| 保持期限を越えた受付/Rawの喪失 | API再取得で補完。過去分は一時スクリプトによる再構築を別途判断 |

費用停止時は`fitbit_processing_paused=true`でAPI・DB処理と定期受付の作成を止める。通知受付は継続する。
停止中のworkerは204を返してタスクを終了し、障害アラートやタスクの再試行を発生させない。
受付記録は未完了のまま保持し、再開後のrepairで再投入する。
保持期限内に復旧し、再開後にrepairを実行する。台帳や受付を削除して再試行を強制しない。
元入力もAPIアクセスも失った期間を完全再構築できるとは保証しない。
変更なしでRawを省いた取得があるため、保持期間内でもRawだけで現在の全期間を再構築できるとは保証しない。
全履歴には一度限りの過去分投入とAPI補完を組み合わせる。

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

## 導入前の確認事項

1. 本番と別のバケット・DB・SAで通知・集中到着・再試行を再現し、反映時間、GCS操作数、Raw増加量、MotherDuck使用量を測る。
   対象のバケットと時間帯を固定し、GCSのAPI操作数、Raw object件数・圧縮後bytesの前後差を取得する。
   アプリlogのAPI取得・省略・新規Raw件数と照合する。MotherDuckは同じ測定区間の使用量画面または請求画面を確認する。
   SQL件数、ローカルDBサイズ、GCS容量だけをAPI操作数や実CUの代用にしない。
2. 既存Screen Timeを含めた月間使用量見込みが無料枠に20%の余裕を残すことを確認する。GCS操作・Raw増加量・MotherDuck実CUを含める。Liteで利用可能な使用量/請求画面を使い、ローカル時間・SQL数やBusiness専用QUERY_HISTORYを実CU計測の代用にしない。
3. 購読管理APIの実行主体・CPEロール・quota projectを確認する。導入前調査では一覧取得が403だったため、現在の権限を再確認する。
4. migrationとdbt Viewを明示的に適用し、OAuth owner一致、端末一覧取得に必要な`googlehealth.settings.readonly` scope、Webhook署名、OIDC、IAM、実際の保持設定、監視を隔離環境で検証する。
5. [過去分の一度限りの投入](#過去分の一度限りの投入)を実施する。
6. PDP専用`pdp-fitbit`購読を対象5種類だけで登録し、ZIP以降から購読開始までの区間をAPI補完する。切替区間を重ねて照合する。
7. 実端末の同期から分析ビューまで確認する。目標はWebhook受付から5分以内で、端末→Google同期時間は含めない。現時点では未測定。

現行想定はproject `health-data-pipeline-503813` / `us-central1`、Raw bucket `health-data-pipeline-503813-pdp-raw`。
旧`health-data-pipeline-dispatch` queueと`health-data-pipeline-hourly` SchedulerはこのTerraformの対象外。
旧構成の停止状態を引き継ぎ資料だけで断定せず、切替前に確認する。
予算通知は厳密な課金上限ではない。無料枠に収まらなければ測定結果に基づいて運用条件を再決定する。

[MotherDuck料金](https://motherduck.com/docs/about-motherduck/billing/pricing/)と
[使用量の確認](https://motherduck.com/docs/about-motherduck/billing/monitoring-usage/)を参照する。
