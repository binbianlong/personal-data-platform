# Fitbit運用

## 現在の状態

2026-09-27時点でローカル実装とオフライン検証を完了した。クラウド適用、PDP購読登録、本番ZIP投入、
実端末から分析までの到達、MotherDuck実CU使用量は未検証。Terraformは`enable_fitbit_runtime=false`、
有効化時も`fitbit_processing_paused=true`が既定。

検証記録:

- 2026-09-27: Ruff・strict mypy・pytest 505件、Terraform fmt/validate/mock test 11件、wheelビルドを確認した。
- 2026-09-28: Ruff・整形・strict mypy・pytest 511件の成功を確認した。
- コンテナ実行は未確認。2026-09-27の検証時はDocker Engineが起動していなかった。

これらはローカルの検証結果であり、クラウド動作・実CUの検証とは分けて扱う。

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
| `PDP_FITBIT_OAUTH_CLIENT_ID` / `PDP_FITBIT_OAUTH_CLIENT_SECRET` / `PDP_FITBIT_OAUTH_REFRESH_TOKEN` | API OAuth |
| `PDP_FITBIT_HEALTH_USER_ID` | Webhookの対象owner。OAuthのownerと一致させる |
| `PDP_FITBIT_WEBHOOK_AUTHORIZATION` | 購読に設定するAuthorization共有値 |
| `PDP_FITBIT_SERVICE_URL` | ServiceのHTTPS URL。内部タスクOIDC audienceにも使う |
| `PDP_FITBIT_TASK_SERVICE_ACCOUNT` | タスク呼出用SA email |
| `PDP_FITBIT_TASKS_PARENT` | `projects/.../locations/.../queues/pdp-fitbit` |
| `PDP_FITBIT_PROCESSING_PAUSED` | `true`でworkerと補修のAPI/DB更新を停止 |
| `PDP_FITBIT_REPAIR_ENABLED` | `true`で受付再投入と日次補修を有効化 |
| `GOOGLE_CLOUD_PROJECT` / `GCS_BUCKET` | 既存GCS設定 |
| `MOTHERDUCK_DATABASE` / `MOTHERDUCK_TOKEN` | 既存DB設定 |

Terraformには5つの既存Secret Manager IDを`fitbit_secret_ids`で渡す。秘密値・秘密versionの本文をstateに持たせない。
MotherDuck secretは既存参照を使う。旧環境リポジトリへの実行時依存はない。

## クラウド構成と定期補修

`enable_fitbit_runtime=true`で専用Service・queue・SA・IAM・Raw lifecycleを追加する。
Cloud Runはmin 0 / max 1、HTTP同時処理16。Cloud Tasksは同時dispatch 1、最大20回/24時間の再試行。
内部workerも既存Screen Timeと同じ`loader` leaseを取得する。ロック競合時は503で再試行する。
異常終了で残ったleaseは期限切れ後に回復する。DB確定結果が不明な接続を再利用しない。

既存のScreen Time reconciliation JobにFitbit補修を接続し、Schedulerを増設しない。
未完了受付を再投入し、Tokyoの日付をkeyとして1日1回、当日を含む直近7日を照合する。
同日の2回目の定期実行は同じ受付記録を再利用する。通常通知に意図的な待ち時間を設けない。
未取込Raw、分析ビューの存在/照会、GCS保持期限を共有reconciliationで監査する。

API Rawと受付記録はGCS作成から30日で削除対象。Rawが33日以降も残る場合は監査失敗とする。
この3日は非同期削除の監査猶予で、33日保持を保証しない。Screen Timeは90日を維持する。
未完了受付が27日以上なら保持期限が近い異常として報告する。
受付の最新時刻・完了時刻・未完了数・最古経過時間を補修logに出し、端末からの通知がないことだけでは障害扱いしない。
Service処理失敗のlog alertと既存reconciliation Job監視を利用する。

## 復旧と停止

| 状態 | 復旧 |
|---|---|
| queue登録失敗・再試行終了 | 残った受付を定期repairで再投入 |
| Raw保存後に処理停止 | 保存済み参照を使って同じgenerationを再試行。未参照Rawは定期監査で取り込む |
| DB commit結果不明 | 接続を破棄。新接続で取込台帳を確認し、成功済みなら再書込を省略 |
| OAuth失効・権限不足 | secret/owner/権限を確認し、復旧後に受付を再投入 |
| API制限・一時障害 | Cloud Tasksのbackoffで再試行。途中結果は保存しない |
| 長期停止・7日より前の修正 | `sync --from ... --to ...`で指定期間を補完 |
| 保持期限を越えた受付/Rawの喪失 | API再取得で補完。過去分は一時スクリプトによる再構築を別途判断 |

費用停止時は`fitbit_processing_paused=true`でAPI・DB処理を止める。通知受付は継続する。
停止中のworkerは204を返してタスクを終了し、障害アラートやタスクの再試行を発生させない。
受付記録は未完了のまま保持し、再開後のrepairで再投入する。
保持期限内に復旧し、再開後にrepairを実行する。台帳や受付を削除して再試行を強制しない。
元入力もAPIアクセスも失った期間を完全再構築できるとは保証しない。
Rawだけの共通rebuildは保持期間内が対象で、全履歴には一度限りの過去分投入とAPI補完を組み合わせる。

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
2. 既存Screen Timeを含めた月間使用量見込みが無料枠に20%の余裕を残すことを確認する。GCS操作・Raw増加量・MotherDuck実CUを含める。Liteで利用可能な使用量/請求画面を使い、ローカル時間・SQL数やBusiness専用QUERY_HISTORYを実CU計測の代用にしない。
3. 購読管理APIの実行主体・CPEロール・quota projectを確認する。導入前調査では一覧取得が403だったため、現在の権限を再確認する。
4. migrationとdbt Viewを明示的に適用し、OAuth owner一致、Webhook署名、OIDC、IAM、実際の保持設定、監視を隔離環境で検証する。
5. [過去分の一度限りの投入](#過去分の一度限りの投入)を実施する。
6. PDP専用`pdp-fitbit`購読を対象5種類だけで登録し、ZIP以降から購読開始までの区間をAPI補完する。切替区間を重ねて照合する。
7. 実端末の同期から分析ビューまで確認する。目標はWebhook受付から5分以内で、端末→Google同期時間は含めない。現時点では未測定。

現行想定はproject `health-data-pipeline-503813` / `us-central1`、Raw bucket `health-data-pipeline-503813-pdp-raw`。
旧`health-data-pipeline-dispatch` queueと`health-data-pipeline-hourly` SchedulerはこのTerraformの対象外。
旧構成の停止状態を引き継ぎ資料だけで断定せず、切替前に確認する。
予算通知は厳密な課金上限ではない。無料枠に収まらなければ測定結果に基づいて運用条件を再決定する。

[MotherDuck料金](https://motherduck.com/docs/about-motherduck/billing/pricing/)と
[使用量の確認](https://motherduck.com/docs/about-motherduck/billing/monitoring-usage/)を参照する。
