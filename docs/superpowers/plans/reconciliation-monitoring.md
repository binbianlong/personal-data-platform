# Reconciliation・Recovery・Monitoring 実装計画

> **エージェント実装時の必須事項:** この計画をタスク単位で実装する際は、`superpowers:subagent-driven-development`（推奨）または `superpowers:executing-plans` を使用する。進捗管理にはチェックボックス（`- [ ]`）を使用する。

**目標:** Webhook取りこぼし、B2→MotherDuck取込漏れ、Reconciliation自体の停止を独立経路で検知し、自動修復とEmail通知を行える運用層を実装する。

**アーキテクチャ:** Daily Cloud Run Reconciliation Jobがsource↔B2、B2↔MotherDuckを順番に照合し、不足分を自動Backfill/Loader再実行する。Jobが完全成功した場合だけHealthchecks.ioへheartbeatを送る。Cloud Logging/Monitoringは明示的失敗をEmail通知し、Healthchecks.ioはJob未実行・silent failureを検知する。

**技術スタック:** Python, Cloud Run Jobs, Cloud Scheduler, Google Cloud Monitoring/Logging, Cloud Tasks, Backblaze B2, MotherDuck, Healthchecks.io, Terraform, pytest

**設計spec:** `docs/superpowers/specs/2026-08-20-personal-data-platform-design.md`

## 全体制約

- Reconciliationはfreshnessの通常経路ではなくcorrectnessの保険。
- Google Healthの通常更新はWebhookを使い、高頻度Pollingへ戻さない。
- Reconciliationは低頻度の日次実行。
- Heartbeatは全整合性処理が成功した後にだけ送る。
- Manual Backfillは最終手段であり通常復旧経路にしない。
- MotherDuck全損時はB2だけからbase・ingestion metadata・martsを再構築できる。
- Raw replay時はobject keyの`observed_at`を利用して古い順に処理する。
- PaginationRequiredError等で範囲取得が完全でない場合はheartbeatを送らない。

---

## ファイル構成

```text
src/personal_data_platform/
├─ reconciliation/
│  ├─ job.py
│  ├─ source_audit.py
│  ├─ ingestion_audit.py
│  ├─ heartbeat.py
│  └─ models.py
├─ recovery/
│  └─ rebuild.py
└─ storage/
   ├─ b2.py
   └─ motherduck.py

infra/terraform/
├─ monitoring.tf
└─ scheduler.tf

tests/
├─ unit/
│  ├─ test_reconciliation_diff.py
│  ├─ test_ingestion_audit.py
│  └─ test_heartbeat.py
└─ e2e/
   └─ test_reconciliation_recovers_missing_load.py
```

### タスク1: Reconciliation結果modelと失敗semantics

**対象ファイル:**
- 作成: `src/personal_data_platform/reconciliation/models.py`
- テスト: `tests/unit/test_reconciliation_diff.py`

**インターフェース:**
- 提供: `AuditResult`
- 提供: `ReconciliationFailed`

- [ ] **ステップ1: status testを書く**

```python
from personal_data_platform.reconciliation.models import AuditResult

def test_full_success_requires_every_stage():
    result = AuditResult(
        source_to_b2_ok=True,
        b2_to_motherduck_ok=True,
        analytics_ok=True,
    )
    assert result.is_success is True

def test_any_failed_stage_blocks_heartbeat():
    result = AuditResult(
        source_to_b2_ok=True,
        b2_to_motherduck_ok=False,
        analytics_ok=True,
    )
    assert result.is_success is False
```

- [ ] **ステップ2: immutable result modelを実装する**

frozen dataclassまたはPydantic modelを使用し、`is_success`は手動設定せず計算値にする。

- [ ] **ステップ3: Commit**

```bash
git add src/personal_data_platform/reconciliation/models.py tests/unit/test_reconciliation_diff.py
git commit -m "feat: define reconciliation success contract"
```

### タスク2: Source ↔ B2 auditと自動Backfill

**対象ファイル:**
- 作成: `src/personal_data_platform/reconciliation/source_audit.py`
- 変更: `src/personal_data_platform/sources/google_health/client.py`
- 変更: `src/personal_data_platform/storage/b2.py`
- テスト: `tests/unit/test_source_audit.py`

**インターフェース:**
- 提供: `audit_google_health_day(day: date) -> SourceAuditResult`
- 使用: same Raw identity builder as normal webhook Fetch Worker

- [ ] **ステップ1: Daily Reconciliationの取得sliceを固定する**

初期対応data typeでは以下のbounded query sliceを使用する:

```text
steps      → 1-hour physical ranges
heart-rate → 1-hour physical ranges
sleep      → 1-day physical range
```

responseを想定page上限以内に抑える。いずれかのqueryで`nextPageToken`が返った場合は、部分snapshotを保存せずauditを失敗させる。

- [ ] **ステップ2: Raw欠損テストを書く**

Reconciliationによる取得はすべて`operation="SNAPSHOT"`として保存する。対象scopeのSNAPSHOT RawがB2にない場合、通常経路と同じB2保存処理を呼び、Loaderをenqueueする。

- [ ] **ステップ3: 既存snapshotのテストを書く**

同じlogical rangeで直前SNAPSHOT bytesが同一なら、重複objectやLoader Taskを作成しない。

- [ ] **ステップ4: 昨日 + 短いlookbackのauditを実装する**

初期window:

```text
yesterday
previous day
```

2日overlapにより、Reconciliationを連続Pollingへ変えずに遅延訂正を拾う。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/reconciliation/source_audit.py \
  src/personal_data_platform/sources/google_health/client.py \
  src/personal_data_platform/storage/b2.py tests/unit/test_source_audit.py
git commit -m "feat: reconcile google health raw coverage"
```

### タスク3: B2 ↔ MotherDuck ingestion audit

**対象ファイル:**
- 作成: `src/personal_data_platform/reconciliation/ingestion_audit.py`
- 変更: `src/personal_data_platform/storage/b2.py`
- 変更: `src/personal_data_platform/storage/motherduck.py`
- テスト: `tests/unit/test_ingestion_audit.py`

**インターフェース:**
- 提供: `find_uningested_objects(prefix: str) -> list[str]`

- [ ] **ステップ1: 集合差分テストを書く**

```python
def test_finds_b2_object_missing_from_ingestion_metadata():
    b2 = {"raw/a", "raw/b"}
    ingested = {"raw/a"}
    assert diff_uningested(b2, ingested) == ["raw/b"]
```

- [ ] **ステップ2: B2 Raw観測listingを実装する**

repository listingは以下を返す:

```python
@dataclass(frozen=True)
class RawObservationRef:
    key: str
    observed_at: datetime
    operation: str
    sha256: str
```

Replay順は`(observed_at, key)`の昇順にする。`observed_at`はRaw object keyから復元する。

- [ ] **ステップ3: succeeded ingestion metadataをqueryする**

`status='succeeded'`だけを取込完了として扱う。

- [ ] **ステップ4: 未取込Loader Taskをenqueueする**

SHA-256(object key)に基づく決定的Task名を使い、`ALREADY_EXISTS`はすでにqueue済みとして扱う。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/reconciliation/ingestion_audit.py \
  src/personal_data_platform/storage tests/unit/test_ingestion_audit.py
git commit -m "feat: repair missed raw loads"
```

### タスク4: Analytics整合性check

**対象ファイル:**
- 作成: `src/personal_data_platform/reconciliation/job.py`
- テスト: `tests/unit/test_reconciliation_job.py`

**インターフェース:**
- 提供: `run_reconciliation() -> AuditResult`

- [ ] **ステップ1: 実行順序テストを書く**

call順序を確認する:

```text
source audit
→ ingestion audit
→ analytics validation
→ heartbeat
```

3段階すべてが成功する前にHeartbeatを送ってはならない。

- [ ] **ステップ2: 軽量なAnalytics validationを実装する**

Check:

```text
required base tables exist
required mart Views exist
latest expected day is queryable
no failed ingestion_metadata rows remain in the audited window
```

martsがViewである間はReconciliationごとにdbtを実行しない。

- [ ] **ステップ3: いずれかのstage失敗でCloud Run Jobを失敗させる**

構造化された失敗contextをlogした後にexceptionを送出し、Cloud Run Job execution statusをfailedにする。

- [ ] **ステップ4: Commit**

```bash
git add src/personal_data_platform/reconciliation/job.py tests/unit/test_reconciliation_job.py
git commit -m "feat: orchestrate daily reconciliation"
```

### タスク5: Healthchecks.io heartbeat

**対象ファイル:**
- 作成: `src/personal_data_platform/reconciliation/heartbeat.py`
- テスト: `tests/unit/test_heartbeat.py`

**インターフェース:**
- 提供: `send_success_ping(url: str) -> None`

- [ ] **ステップ1: HTTP testを書く**

`respx`を使用し、設定済みping URLへ成功GET/POST requestがちょうど1回送られることを確認する。

- [ ] **ステップ2: 厳密なsuccess semanticsを実装する**

non-2xx responseはすべて`HeartbeatError`とする。

main Reconciliation Jobは、すべてのcheckとrepair完了後にのみこれを呼ぶ。

- [ ] **ステップ3: `reconciliation` entrypointへ接続する**

`python -m personal_data_platform.entrypoint reconciliation`はJobを1回実行し、完全成功時のみexit 0とする。

- [ ] **ステップ4: Commit**

```bash
git add src/personal_data_platform/reconciliation src/personal_data_platform/entrypoint.py tests
git commit -m "feat: add external reconciliation heartbeat"
```

### タスク6: MotherDuck完全rebuild command

**対象ファイル:**
- 作成: `src/personal_data_platform/recovery/rebuild.py`
- テスト: `tests/unit/test_rebuild_order.py`

**インターフェース:**
- 提供: `rebuild_motherduck_from_b2() -> None`

- [ ] **ステップ1: replay順序テストを書く**

同一logical scopeに異なる`observed_at`のRaw観測が2件ある場合、古い観測から処理されることを確認する。

- [ ] **ステップ2: destructive rebuild guardを実装する**

明示的に以下のenvironment variableを必須とする:

```text
ALLOW_MOTHERDUCK_REBUILD=true
```

設定されていなければerror終了する。

- [ ] **ステップ3: rebuild sequenceを実装する**

```text
apply empty base schema
→ list all B2 Raw
→ sort by LastModified then key
→ replay each object through Loader domain logic without Cloud Tasks
→ rebuild ingestion_metadata
→ execute dbt-runner once
→ dbt test
```

Loader functionを再利用し、2つ目のparse実装を作らない。

- [ ] **ステップ4: dry-run modeを追加する**

dry-runでは以下を出力する:

```text
object count
source counts
data type counts
oldest/newest observed_at
```

dry-runでは書き込みを一切行わない。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/recovery tests/unit/test_rebuild_order.py
git commit -m "feat: rebuild motherduck from b2"
```

### タスク7: Cloud Monitoring Email alert

**対象ファイル:**
- 作成: `infra/terraform/monitoring.tf`
- 変更: `infra/terraform/variables.tf`
- テスト: Terraform validation

**インターフェース:**
- 提供: email notification channel
- 提供: alert policies for explicit failures

- [ ] **ステップ1: alert Email variableを追加する**

```hcl
variable "alert_email" {
  type      = string
  sensitive = true
}
```

- [ ] **ステップ2: notification channelを作成する**

`google_monitoring_notification_channel`をtype `email`で作成する。

- [ ] **ステップ3: alert policyを追加する**

以下のpolicyを作成する:

```text
Cloud Run Job reconciliation failed
Cloud Run Job dbt-runner failed
health-fetch 5xx/error log condition
health-loader 5xx/error log condition
Cloud Tasks repeated failure/dead processing symptom available through chosen metric/log query
```

利用可能ならdirect service metricを優先し、direct metricで表現できない障害だけlog-based alertを使う。

- [ ] **ステップ4: Terraformをvalidateする**

```bash
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
```

- [ ] **ステップ5: commitする**

```bash
git add infra/terraform/monitoring.tf infra/terraform/variables.tf
git commit -m "infra: alert on pipeline failures"
```

### タスク8: Reconciliation復旧E2E

**対象ファイル:**
- 作成: `tests/e2e/test_reconciliation_recovers_missing_load.py`

**インターフェース:**
- 検証: 主要なsilent failure復旧保証

- [ ] **ステップ1: ingestion metadataなしのRaw B2 objectをseedする**

専用test prefix/databaseを使用する。

- [ ] **ステップ2: Reconciliationを実行する**

Job functionまたはstaging Cloud Run Jobを実行する。

- [ ] **ステップ3: 復旧を確認する**

以下を検証する:

```text
base row now exists
ingestion_metadata status = succeeded
no duplicate Raw object created
heartbeat was sent only after recovery
```

- [ ] **ステップ4: 高速PR testとは分離してE2Eを実行する**

```bash
pytest tests/e2e/test_reconciliation_recovers_missing_load.py -v
```

Unit Test編集のたびではなく、main/manual workflowで実行する。

- [ ] **ステップ5: commitする**

```bash
git add tests/e2e
git commit -m "test: verify reconciliation repairs missed loads"
```

## 計画全体の検証

最終検証:

```bash
pytest tests/unit tests/integration tests/e2e -v
ruff check src tests
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
```

運用受入検証:

```text
1. Disable/skip one Loader task intentionally in staging.
2. Confirm B2 Raw remains present.
3. Run reconciliation.
4. Confirm MotherDuck is repaired.
5. Confirm Healthchecks heartbeat is sent.
6. Force reconciliation failure.
7. Confirm heartbeat is absent and GCP email alert fires.
```

ここまでで初期platform coreは完成とする。SwitchBot、天気、Screen Time、位置情報、日記/ChatGPT要約collectorは、取得方式・schema・freshness要件が独立しているため、それぞれsource固有のspecと実装計画を作る。
