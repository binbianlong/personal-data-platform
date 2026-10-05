# Webhookを維持する最小構成への改修・西部移行計画

**目的:** Screen TimeとFitbitの収集・保存・分析を維持し、通知管理・自動復旧・常設運用資産を減らす。

**構成:** Webhook receiverとPub/Subを残し、通知を日付×種別に分ける。毎時Jobは再取得可能な単位を処理し、日次JobはScreen Time・7日再照合・dbt・監査を行う。外部欠損監視は日次1件とする。

**技術:** Python 3.13+、Google Health v4、Cloud Run、Pub/Sub、GCS、DuckDB/MotherDuck、dbt、Terraform。

**設計:** [最小構成の設計書](../specs/2026-10-05-pubsub-gcs-migration-design.md)

**状態:** 2026-10-06改訂。下記の最小構成は未実装。本番切替は準備段階で止めている。旧計画の実装完了は、この計画の完了を意味しない。

## 共通条件

- 通常runtimeはGCP `us-west1`、MotherDuck `us-west-2`。受信Service 1、Pub/Sub topic/subscription各1、処理Job 2、Scheduler 2。
- 毎時は15分開始・最大50分。日次は04:10 Asia/Tokyo開始・最大100分。共通`loader` leaseは125分。
- 受信bodyは1 MiB、展開は1リクエスト1,000単位まで。pull最大500、収集待ち最大120秒、ack延長1回最大600秒。
- 物理時刻はTokyo日境界、civil dateは提供元の日付。心拍は60秒rollUpの平均・最小・最大、sample count NULL。
- Rawはgzip・90日保持・create-only・hash/generation照合。最小Fitbit Rawは`raw/fitbit/v3/`、1 object最大16 MiB。
- 保持するFitbit運用表はcoverage 2表と削除記録。通知・attempt・bundle・cursor等の10表は新しいmigrationで撤去する。
- 完全な空取得と途中失敗を区別し、新しい更新・削除を古いRawで巻き戻さない。Screen Timeのデータ・parser・削除処理を維持する。
- HealthchecksはPeriod 24時間＋Grace 24時間の1件。日次の全段階が完了するまで成功を送らない。
- 旧DBのmigration台帳・適用済みSQLを変更しない。state、token、Raw、健康データをGitへ入れない。

## 確認する境界

1. 日境界・civil date・時差なし通知・現在日の未完了分を誤って欠落または全削除にしない。
2. 発行途中で503になって再送された通知を受け入れ、DB commit不明やack消失でも二重計上しない。
3. Raw保存後に止まっても再取り込みでき、古いRawが更新済みID・削除を巻き戻さない。
4. 広い通知や一部API失敗で成功済み単位を毎回やり直すだけにならず、未処理単位が残る。
5. lease競合や日次の一段階の失敗を外部成功にせず、検証資格情報を本番DBへ使わない。

## 準備済みのものと再利用方針

[移行記録](../../platform/west-migration-2026-10-05.md)の資産・backupを使う。別の環境や新規の移行frameworkは作らない。

| 準備済み | 扱い |
| --- | --- |
| 西部state/registry、Raw/preflight bucket、Pub/Sub、receiver、Logging bucket | IDとstateを維持して利用。新たなJobを作る前にplanを確認 |
| Screen Time Raw 53 objectと9表243,898行の初回snapshot | 再利用し、writer/collector停止後の最終差分を追加 |
| 旧DBの4 migration checksum | 読み取り・保護。新台帳をコピーしない |
| 西部production/preflightの独立DBとPulse service accounts | 001〜004適用済み。005を追加。preflightは手動試験用 |
| 西部Secret Managerの5 container | OAuth/Webhookのversion 1のみ登録済み。通常runtimeが必要な4 payloadだけ参照 |
| Healthchecksのaccount/APIキー | 日次1件の設定に使用。runtimeへ管理キーを渡さない |
| 旧設計のimage `66a090…` | 準備時点の証拠として保管。Raw v3のrollbackには最小構成の新imageを使う |

旧2 Schedulerは停止中、西部Jobは0件、provider URLとMac collectorと分析/MCPは旧接続先のまま。データ削除・最終切替は未実施。初回backupを最終差分として扱わない。

## 1. 通知を小さい処理単位へ分割する

**変更:** `src/personal_data_platform/sources/fitbit/webhook.py`、`src/personal_data_platform/sources/fitbit/service.py`、`src/personal_data_platform/sources/fitbit/notifications.py`、`src/personal_data_platform/sources/fitbit/models.py`。
**テスト:** `tests/unit/test_fitbit_webhook.py`、`tests/unit/test_fitbit_notifications.py`、`tests/integration/test_fitbit_service.py`。

**インターフェース:** `split_notifications(notification: Notification, *, max_units: int = 1000) -> tuple[Notification, ...]`をwebhook.pyへ追加する。出力は既存Notification型、`windows`が1要素・1日以内。子IDは元IDと日付/種別から得たhashで128文字以内。receiverは全groupsを展開し、総数上限を検証してから発行する。

- [ ] **失敗するテストを書く。** `test_notification_splits_across_tokyo_days`はUTC 2026-10-01 14:00〜10-02 16:00の歩数通知がTokyoの3日に分かれることを確認する。睡眠のcivil日境界、時差なし通知、物理時刻で完全に未来の日、128文字の元IDも同じhelperで検証する。
- [ ] **発行契約のテストを書く。** `test_receiver_rejects_expansion_before_publishing`は1,001単位で400・発行0件。`test_partial_publish_returns_503_and_retry_preserves_units`は途中失敗後の再送で全単位が発行され、重複を許容する。認証・署名・ユーザー・5種別・handshakeの既存テストを残す。
- [ ] `python -m pytest tests/unit/test_fitbit_webhook.py tests/unit/test_fitbit_notifications.py tests/integration/test_fitbit_service.py -q`で新しい期待に対するFAILを確認する。
- [ ] helperとreceiverを実装し、consumerに1単位のenvelope検証を追加する。既存のAPI取得はreceiverへ入れない。全展開の前にbodyを検証し、成功発行の完了後だけ204。
- [ ] 同じテストをPASSにし、commit `refactor: Fitbit通知を日付と種別の処理単位へ整理`。

## 2. 取得・Raw・DB反映を共通Loader中心にする

**変更:** `src/personal_data_platform/sources/fitbit/acquisition.py`、`src/personal_data_platform/sources/fitbit/models.py`、`src/personal_data_platform/sources/fitbit/raw.py`、`src/personal_data_platform/sources/fitbit/adapter.py`、`src/personal_data_platform/sources/fitbit/writer.py`、`src/personal_data_platform/sources/fitbit/runtime.py`。
**追加:** `src/personal_data_platform/migrations/west/005_minimal_fitbit_processing.sql`。
**撤去:** `src/personal_data_platform/sources/fitbit/acquisition_state.py`の通知・attempt・bundle・cursor管理と、その通常経路の参照。
**テスト:** `tests/integration/test_fitbit_acquisition.py`、`tests/integration/test_fitbit_acquisition_state.py`、`tests/integration/test_west_migration_baseline.py`、`tests/integration/test_fitbit_writer.py`、`tests/integration/test_fitbit_loader.py`、`tests/unit/test_fitbit_bundle.py`。

**インターフェース:** `AcquisitionRunner.ingest(queue, *, max_messages=500, collect_seconds=120, timeout_seconds=3000) -> AcquisitionSummary`と`run_windows(windows, *, warehouse, lease_owner, timeout_seconds) -> AcquisitionSummary`を維持する。Task 1の単位をメモリ内でgroupingする。`FitbitBundle.entries`は`tuple[CapturedSnapshot | HeartRateMinuteSnapshot, ...]`とし、attemptとchunkの永続状態を持たせない。`encode_bundle(bundle: FitbitBundle) -> tuple[tuple[str, bytes], ...]`は完全取得の境界だけで16 MiB以内に分ける。

- [ ] **失敗するテストを書く。** 通知台帳10表なしで取得→Raw→分析/coverage/取込metadataのcommit→ackが動くことを確認する。範囲が同じ・内容変更なしならRaw追加0件、A→B→Aなら3観測を保持する。未知API項目の変更、完全な空結果、途中ページ失敗はそれぞれ変更・削除・未完了として区別する。
- [ ] **停止と進捗のテストを書く。** `test_redelivery_after_unknown_commit_is_idempotent`、`test_saved_raw_recovers_without_attempt_tables`、`test_scope_failure_does_not_block_other_days`を追加する。失敗単位はackされず、成功単位だけackされる。次回は未処理日を処理できることを実DBで確認する。
- [ ] **変更なしの保護をテストする。** `test_pending_raw_is_loaded_before_unchanged_fetch`はA反映→BのRaw保存直後に停止→A再取得の順で、Bを先に再生し、Aを新Rawとして保存することを確認する。`test_unchanged_fetch_prevents_stale_raw_replay`は同じ内容の再取得後に古いRawを再生しても、更新・moved ID・削除を巻き戻さないことを確認する。
- [ ] **writerの既存保護を確認する。** moved ID、削除後の古いRaw、現在日での心拍分境界、generation違い、lease喪失に対するテストを維持する。Raw codecはv3の直接gzip JSONで往復し、base64連結や欠けたchunkの補修を提供しない。
- [ ] **Raw上限のテストを書く。** `test_bundle_splits_only_between_complete_entries`は圧縮16 MiB以内で完全取得の境界だけに分かれ、各objectを単独でdecode/loadできることを確認する。`test_oversized_scope_does_not_block_small_scopes`は単一取得が上限超過ならその単位だけ未完了とし、他の単位は保存・commit・ackできることを確認する。
- [ ] `python -m pytest tests/integration/test_fitbit_acquisition.py tests/integration/test_fitbit_acquisition_state.py tests/integration/test_west_migration_baseline.py tests/integration/test_fitbit_writer.py tests/integration/test_fitbit_loader.py tests/unit/test_fitbit_bundle.py -q`で未対応のFAILを確認する。
- [ ] 空ではないpullで保存済みの未反映Rawを先に一度共有Loaderで再試行し、fresh API取得の後の成功だけackする。未反映Rawが残る範囲は保留する。通常取得のRaw bytesをLoaderへ直接渡す。変更なしの取得でも既存のcoverage・対象IDの更新/削除保護の取得時刻をtransactionで進め、Raw参照は維持する。新しい専用tableを作らない。
- [ ] 005で10表を削除する。削除前に対象の状態が空、profileがwest、writerなしを確認する。001〜004を変更しない。`test_minimal_migration_preserves_applied_checksums_and_screen_time`で既存データ・台帳・coverageが残り、10表だけ消えること、再適用が安全なことを確認する。
- [ ] テストをPASSにし、commit `refactor: Fitbit取得を再取得とRaw取り込みへ簡素化`。

## 3. 2つのJobと1つの欠損監視に絞る

**変更:** `src/personal_data_platform/sources/fitbit/runtime.py`、`src/personal_data_platform/cli.py`、`src/personal_data_platform/reconciliation/job.py`、`src/personal_data_platform/reconciliation/heartbeat.py`、`src/personal_data_platform/dbt_runner.py`、`infra/terraform/west.tf`、`infra/terraform/west_monitoring.tf`、`infra/terraform/variables.tf`、`infra/terraform/secrets.tf`、`infra/terraform/tests/west.tftest.hcl`、`docs/sources/fitbit/operations.md`、`docs/platform/operations.md`。
**テスト:** `tests/integration/test_reconciliation_job.py`、`tests/unit/test_fitbit_runtime.py`、`tests/unit/test_fitbit_cli.py`、Terraform west contract。

**インターフェース:** `run_sync_from_env(*, start: datetime, end: datetime, data_types: tuple[str, ...] = DATA_TYPES) -> int`は期間指定必須、永続resume引数なし。`daily_heartbeat_urls() -> dict[str, str]`は`{"daily": HTTPS_URL}`だけを返す。日次は既存`run_windows`へ直近7完了日を渡し、内部Loader/dbtへ同じlease ownerと残り期限を渡す。

- [ ] **失敗するテストを書く。** 日次の各Loader・API・dbt・両stream監査が失敗/保留なら外部成功0回、すべて成功なら1回。共通のstream監査/成功記録は残る。leaseがbusyなら処理と成功通知を進めない。7日より古い停止区間は手動補修が必要な期間としてJob記録に残す。
- [ ] **手動取得のテストを書く。** `--from`/`--to`必須、`--resume-id`は拒否。同じ期間の再実行は冪等、取得失敗でデータを全削除しない。指示した古い日付を直近7日へ切り詰めず、時間指定は日全体へ拡大しない。途中終了時は最初の未完了日・種別を結果へ出す。
- [ ] `python -m pytest tests/integration/test_reconciliation_job.py tests/unit/test_fitbit_runtime.py tests/unit/test_fitbit_cli.py -q`と`terraform -chdir=infra/terraform test -filter=tests/west.tftest.hcl`でFAILを確認する。
- [ ] 日次の保存済みRaw再試行→7日API再照合→dbt→監査→外部1 pingを実装する。collectorの24時間control、両streamの48時間監査、完成済みsegment保留は変更しない。
- [ ] `west_jobs`をhourly/dailyの2件にする。runtime SAを共用し、receiver SAを分離。RawのIAM・一覧・Lifecycle対象を`raw/fitbit/v3/`へ揃える。Schedulerは毎時15分と日次04:10、準備中はpaused。専用preflight/dbt Jobを定義しない。
- [ ] runtimeのsecret参照を本番MotherDuck・OAuth・Webhook・daily heartbeatの4 payloadへ限定する。preflight token containerは準備済み未使用資産として保持し、数値version必須条件は実際のruntime参照4 keyに対応させる。空containerの存在だけでversion作成を強制しない。
- [ ] native監視は2 Job失敗をまとめる1 policy、24時間のPub/Sub滞留1 policy、receiver ERROR log 1 policyにする。重複したJob ERROR metric/alertと日次成功metricを撤去する。Healthchecksは既定の未使用チェックを日次1件へ設定し、24h＋24h・自分のemail通知を確認する。
- [ ] Python/Terraform testをPASSにし、commit `refactor: 西部Jobと日次監視を最小構成へ統合`。

## 4. 最小releaseを検証し、停止状態で準備する

**変更:** 必要なCI/package設定、`scripts/migrate_west_warehouse.py`、`scripts/migrate_west_raw.py`、`docs/platform/west-migration-2026-10-05.md`。
**成果:** Raw v3対応image、source/targetを分離した移行経路、復元結果、pausedの2 Job。

- [ ] Ruff check/format、mypy、`python -m pytest -q`、package build、linux/amd64 container smoke、各Terraform rootのfmt/validate/test、`git diff --check`を実行する。原計画向けの台帳テストはTask 2の最小契約へ改訂し、必要な障害・データ保護のテストを残す。
- [ ] 新DBで001〜005の適用、source export→別processのtarget import、全値digestとRaw generation/保持起点、0 active leaseを確認する。同一processで異なるMotherDuck tokenを使う`--final-delta`経路は分離実行へ直すか廃止し、通常のexport/importを使用する。
- [ ] 手動preflightを独立DB/tokenとpreflight bucketで実施し、検証tokenで本番DBへ接続できないことを確認する。新たな常設Jobは作らない。
- [ ] 5種別の限定1完了日を実API取得し、Raw v3・分心拍・空結果・dbtを確認する。別の空DBへScreen Time backupと保存Rawを復元する試験を1回行う。既存の初回snapshotを最終差分とは扱わない。
- [ ] 最小releaseのcommit/image digestと復元先を記録し、停止状態の2 Jobだけdeployする。Terraform planは保護bucketの置換、旧resourceの意図しない変更/削除、予定外のJob/metric作成がないことを確認して適用する。
- [ ] Healthchecksの短い試験周期で欠損→通知→成功による復旧を確認し、24h＋24hへ戻す。native警報も発火/復旧を確認する。メールの到達を未確認なら明記する。
- [ ] commit `fix: 最小構成の移行と復元を検証可能にする`。cloud検証の結果は状態・時刻・件数だけ記録する。

## 5. 最終差分・切替・旧経路の整理

**変更:** 旧receiverの一時設定、collector plist/runtime、Terraform/CIの定常参照、`src/personal_data_platform/sources/fitbit/service.py`、`src/personal_data_platform/sources/fitbit/runtime.py`、`src/personal_data_platform/sources/fitbit/receipts.py`、`src/personal_data_platform/sources/fitbit/sync_state.py`と旧依存/fixture/docs、`scripts/cleanup_fitbit_legacy.py`。
**成果:** 最小経路だけで収集・分析でき、旧writerは停止、旧Fitbit資産は範囲限定で整理される。

- [ ] 旧URLも最小releaseのreceiverへ更新し、Task 1と同じ日付×種別の単位で西部Pub/Subへ発行する。認証/handshakeを確認してprovider URLを西部へ切り替える。新定期writerはまだ開始しない。Pub/Sub保持7日を超える停止には期間指定の再取得を使う。
- [ ] 旧writerの終了とlease 0を確認してcollectorを停止する。Raw・warehouse・SQLiteを再backupし、最終Rawをコピー、source exportとtarget importを別processで行う。Screen Time9表の全値・期間・generationを照合する。
- [ ] collectorを西部へ向けてcontrolを強制公開し再開する。本番DB ownerの資格情報で同じ所有者の分析アカウントへrestricted read-only shareを付与し、分析/MCPを切り替える。preflightには本番shareを付与しない。手動で毎時と日次の成功、scope commit後ack、両streamの監査、1 heartbeatを確認して2 Schedulerを有効化する。通常Loggingを西部へ向ける。
- [ ] 最小releaseでの復元経路と保存済みRaw再生を確認する。旧imageをRaw v3へ使わない。writerを二重稼働させない。
- [ ] 使用元を照合してCloud Tasks、旧receipt/checkpoint、legacy worker/decoder/mode、永続cursor、不要なenv/secret version/常設資産を整理する。旧Fitbit削除はinventoryのtable/prefix/queueだけに限定し、Screen Time・共通台帳・新Pub/Sub/Rawを対象から外す。旧組織の削除を含めない。
- [ ] 適用済みmigrationを保全したまま通常runtimeのmigration/profileをwestだけへ揃える。migration用の一時分岐と旧registry参照は撤去条件を確認して整理する。remoteの西部対応revisionが反映されるまで停止中のTerraform workflowを再開しない。
- [ ] 関連テスト、全Python suite、変更したTerraform rootを確認し、撤去とdocs更新を別commitへ分ける。`refactor: Fitbitの旧経路と不要な運用資産を撤去`、`docs: 最小構成の運用と移行結果を更新`を候補とする。

## 継続運用の確認

初回切替の確認後、7日・30日の実測は通常の監視値/請求情報で確認する。GCSの容量・Class A/B、Raw数、Pub/Subの滞留・再配信、Cloud Run実行時間/インターネット送信、MotherDuck容量/CUhを記録し、無料枠から超過しそうな項目だけ調整する。専用の計測Jobや自動最適化は作らない。

通常の失敗は当該単位の再試行で復旧する。7日超の停止・広い補修は期間指定で行い、その手動作業を省くために通知台帳や自動cursorを再導入しない。
