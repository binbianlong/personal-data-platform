# MotherDuck・dbt Analytics 実装計画

> **エージェント実装時の必須事項:** この計画をタスク単位で実装する際は、`superpowers:subagent-driven-development`（推奨）または `superpowers:executing-plans` を使用する。進捗管理にはチェックボックス（`- [ ]`）を使用する。

**目標:** B2 Rawを型付きMotherDuck baseへ冪等にロードし、dbtでChatGPT分析向けのView martsを作る。

**アーキテクチャ:** LoaderはB2 objectを展開・parseしてsourceごとのtyped recordへ変換し、stable keyでMotherDuck baseへupsertする。dbtは意味変換・JOIN・集約を担当し、初期martsはすべてViewとする。データ更新ではdbtを起動せず、model定義変更時だけdbtを実行する。

**技術スタック:** Python, boto3, DuckDB/MotherDuck, dbt-duckdb, SQL, pytest

**設計spec:** `docs/superpowers/specs/2026-08-20-personal-data-platform-design.md`

## 全体制約

- Raw JSONをMotherDuckへそのまま複製しない。
- baseはsource × data typeの型付き物理table。
- baseはstable keyで最新状態へupsertする。
- stable keyへ可変measurement valueを含めない。
- source横断JOIN・timezone統一・集約はdbtで行う。
- martsは初期状態ではView。
- data-triggered dbt refreshと10秒coalescingは初期実装しない。
- MotherDuckはB2から再構築可能でなければならない。

---

## ファイル構成

```text
src/personal_data_platform/
├─ loader/
│  ├─ app.py
│  ├─ handler.py
│  └─ models.py
├─ sources/google_health/
│  ├─ parsers.py
│  └─ stable_keys.py
└─ storage/
   └─ motherduck.py

sql/
└─ base/
   ├─ 001_ingestion_metadata.sql
   ├─ 002_google_health_steps.sql
   ├─ 003_google_health_heart_rate.sql
   └─ 004_google_health_sleep.sql

dbt/
├─ dbt_project.yml
├─ profiles.yml
├─ models/
│  ├─ sources.yml
│  └─ marts/
│     ├─ daily_health.sql
│     ├─ sleep_analysis.sql
│     └─ schema.yml
└─ tests/

tests/
├─ unit/
│  ├─ test_google_health_parsers.py
│  └─ test_stable_keys.py
└─ integration/
   ├─ test_motherduck_upsert.py
   └─ test_dbt_models.py
```

### タスク1: MotherDuck接続とbase schema migration

**対象ファイル:**
- 作成: `src/personal_data_platform/storage/motherduck.py`
- 作成: `sql/base/001_ingestion_metadata.sql`
- 作成: `sql/base/002_google_health_steps.sql`
- 作成: `sql/base/003_google_health_heart_rate.sql`
- 作成: `sql/base/004_google_health_sleep.sql`
- テスト: `tests/integration/test_motherduck_upsert.py`

**インターフェース:**
- 提供: `MotherDuckRepository.connect()`
- 提供: `MotherDuckRepository.apply_base_schema()`

- [ ] **ステップ1: schemaを明示的に定義する**

`ingestion_metadata`:

```sql
CREATE SCHEMA IF NOT EXISTS ops;

CREATE TABLE IF NOT EXISTS ops.ingestion_metadata (
    object_key VARCHAR PRIMARY KEY,
    object_hash VARCHAR NOT NULL,
    source VARCHAR NOT NULL,
    ingested_at TIMESTAMPTZ NOT NULL,
    status VARCHAR NOT NULL CHECK (status IN ('succeeded', 'failed'))
);
```

`base.google_health_steps`:

```sql
CREATE SCHEMA IF NOT EXISTS base;

CREATE TABLE IF NOT EXISTS base.google_health_steps (
    stable_key VARCHAR PRIMARY KEY,
    source_record_id VARCHAR,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    step_count BIGINT NOT NULL,
    platform VARCHAR,
    recording_method VARCHAR,
    raw_object_key VARCHAR NOT NULL
);
```

`base.google_health_heart_rate`:

```sql
CREATE TABLE IF NOT EXISTS base.google_health_heart_rate (
    stable_key VARCHAR PRIMARY KEY,
    source_record_id VARCHAR,
    measured_at TIMESTAMPTZ NOT NULL,
    beats_per_minute BIGINT NOT NULL,
    platform VARCHAR,
    recording_method VARCHAR,
    raw_object_key VARCHAR NOT NULL
);
```

For sleep, store session-level fields first:

```sql
CREATE TABLE IF NOT EXISTS base.google_health_sleep (
    stable_key VARCHAR PRIMARY KEY,
    source_record_id VARCHAR,
    start_time TIMESTAMPTZ NOT NULL,
    end_time TIMESTAMPTZ NOT NULL,
    sleep_type VARCHAR,
    minutes_asleep BIGINT,
    minutes_awake BIGINT,
    platform VARCHAR,
    raw_object_key VARCHAR NOT NULL
);
```

Sleep stage expansion can be a separate typed child table when needed; do not hide stages in a generic Raw JSON column.

- [ ] **ステップ2: MotherDuck接続を実装する**

使用するprefix:

```python
import duckdb

def connect(database: str, token: str):
    return duckdb.connect(f"md:{database}?motherduck_token={token}")
```

- [ ] **ステップ3: schema fileを辞書順に適用する**

`sql/base/*.sql`をfilename順に読み、各fileをtransaction内で実行する。

- [ ] **ステップ4: 専用MotherDuck test database/schemaでIntegration Testを実行する**

4つすべてのtableが存在することを確認する。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/storage/motherduck.py sql tests/integration
git commit -m "feat: define motherduck base schema"
```

### タスク2: Stable key

**対象ファイル:**
- 作成: `src/personal_data_platform/sources/google_health/stable_keys.py`
- テスト: `tests/unit/test_stable_keys.py`

**インターフェース:**
- 提供: `stable_key_for_data_point(data_type: str, point: dict) -> str`

- [ ] **ステップ1: source ID優先をテストする**

```python
def test_source_name_is_preferred_when_present():
    point = {
        "name": "users/u/dataTypes/sleep/dataPoints/abc",
        "sleep": {"interval": {
            "startTime": "2026-08-20T00:00:00Z",
            "endTime": "2026-08-20T08:00:00Z",
        }},
    }
    assert stable_key_for_data_point("sleep", point) == (
        "google_health:sleep:users/u/dataTypes/sleep/dataPoints/abc"
    )
```

- [ ] **ステップ2: source IDがないpointの決定的natural keyをテストする**

source IDがないpointでは以下をnatural key inputとして使用する:

```text
steps:
  interval.startTime + interval.endTime + canonical(dataSource)

heart-rate:
  sampleTime.physicalTime + canonical(dataSource)

sleep:
  interval.startTime + interval.endTime + canonical(dataSource)
```

`canonical(dataSource)`はkeyをsortしたcompact JSONとする。可変measurement valueを含めず、異なるdevice/app由来の同時刻recordを区別する。

`count`、`beatsPerMinute`、sleep summaryなどの可変measurement valueをkeyへ含めない。

- [ ] **ステップ3: SHA-256によるnatural-key hashを実装する**

Canonical input例:

```text
google_health|heart-rate|2026-08-20T00:00:00Z|FITBIT|PASSIVELY_MEASURED
```

返却形式:

```text
google_health:heart-rate:<sha256>
```

- [ ] **ステップ4: 可変値がkeyを変えないことを検証する**

BPMを72から74へ変更してもstable keyが同じであることを確認する。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/sources/google_health/stable_keys.py tests/unit/test_stable_keys.py
git commit -m "feat: add stable analytical keys"
```

### タスク3: 型付きGoogle Health parser

**対象ファイル:**
- 作成: `src/personal_data_platform/loader/models.py`
- 作成: `src/personal_data_platform/sources/google_health/parsers.py`
- テスト: `tests/unit/test_google_health_parsers.py`

**インターフェース:**
- 提供: typed records for `steps`, `heart-rate`, `sleep`
- 使用: exact decompressed Google Health response bytes

- [ ] **ステップ1: steps parseをテストする**

入力:

```json
{
  "dataPoints": [{
    "dataSource": {
      "recordingMethod": "PASSIVELY_MEASURED",
      "platform": "FITBIT"
    },
    "steps": {
      "interval": {
        "startTime": "2026-03-04T07:05:00Z",
        "endTime": "2026-03-04T07:06:00Z"
      },
      "count": "40"
    }
  }]
}
```

期待する型付き値:

```text
step_count = 40
start_time = datetime UTC
end_time   = datetime UTC
```

- [ ] **ステップ2: heart-rate parseをテストする**

読み取るfield:

```text
heartRate.sampleTime
heartRate.beatsPerMinute
```

int64形式の文字列をPython `int`へ変換する。

- [ ] **ステップ3: sleep parseをテストする**

session intervalと利用可能なsummary fieldを読む。存在しないoptional summaryは`None`のままとし、0を補完しない。

- [ ] **ステップ4: data type不一致を拒否する**

`steps`のRaw objectに`heartRate`しか存在しない場合は`RawContractError`を送出する。

- [ ] **ステップ5: commitする**

```bash
git add src/personal_data_platform/loader src/personal_data_platform/sources/google_health/parsers.py tests/unit
git commit -m "feat: parse google health raw into typed records"
```

### タスク4: 冪等なbase upsert

**対象ファイル:**
- 変更: `src/personal_data_platform/storage/motherduck.py`
- テスト: `tests/integration/test_motherduck_upsert.py`

**インターフェース:**
- 提供: `upsert_steps(records)`
- 提供: `upsert_heart_rate(records)`
- 提供: `upsert_sleep(records)`
- 提供: `mark_ingested(object_key, object_hash, source)`

- [ ] **ステップ1: 訂正データのテストを書く**

heart rate BPM 72をinsertした後、同じstable keyでBPM 74をinsertする。

期待結果:

```text
row count = 1
beats_per_minute = 74
```

- [ ] **ステップ2: transaction内MERGEを実装する**

incoming batchをtemporary relation/tableへ置き、`stable_key`でbase tableへ`MERGE INTO`する。

matchした場合は可変analysis columnと`raw_object_key`を更新する。

matchしない場合はinsertする。

- [ ] **ステップ3: ingestion metadataとbase writeをatomicにする**

1 transaction内で以下を実行する:

```text
upsert records
→ mark object succeeded
→ commit
```

metadata writeが失敗した場合はbase変更もrollbackする。

- [ ] **ステップ4: Commit**

```bash
git add src/personal_data_platform/storage/motherduck.py tests/integration/test_motherduck_upsert.py
git commit -m "feat: upsert latest base state"
```

### タスク5: Loader Cloud Run Service

**対象ファイル:**
- 作成: `src/personal_data_platform/loader/handler.py`
- 作成: `src/personal_data_platform/loader/app.py`
- 変更: `src/personal_data_platform/entrypoint.py`
- テスト: `tests/unit/test_loader_handler.py`

**インターフェース:**
- 使用: `{ "object_key": str }`
- 提供: MotherDuck base update
- 提供: `ops.ingestion_metadata` success row

- [ ] **ステップ1: 取込済みobjectのskipをテストする**

`ops.ingestion_metadata`で対象object keyがすでに`succeeded`なら、B2をdownloadせず成功終了する。

- [ ] **ステップ2: object pathをparseする**

Extract:

```text
source
data_type
range_kind
start/end
observed_at
operation
sha256
```

downloadしたRaw bytesから再計算したSHA-256がpath内Hashと一致することを検証する。

- [ ] **ステップ3: UPSERT load pathを実装する**

```text
download
→ gunzip
→ hash verify
→ parse typed records
→ stable keys
→ MotherDuck MERGE
→ ingestion metadata
```

- [ ] **ステップ4: DELETE/SNAPSHOTのrange refresh semanticsを実装する**

For Google Health `DELETE` and reconciliation-generated `SNAPSHOT` Raw, treat the fetched response as current truth for that logical range:

1. select the same time field used by the Google Health query contract:
   - `steps`: interval start time
   - `heart-rate`: sample physical time
   - `sleep`: session end time
2. delete base rows whose selected time falls in `[start, end)`;
3. insert/upsert every record present in the fetched response;
4. mark the Raw object ingested.

For `UPSERT`, do not delete the range; only upsert returned records.

range replacementとmetadata更新は同一MotherDuck transaction内で行う。

- [ ] **ステップ5: private endpointへ接続する**

`POST /tasks/load`を公開する。

- [ ] **ステップ6: テストを実行する**

```bash
pytest tests/unit/test_loader_handler.py tests/integration/test_motherduck_upsert.py -v
```

- [ ] **ステップ7: commitする**

```bash
git add src/personal_data_platform/loader src/personal_data_platform/entrypoint.py tests
git commit -m "feat: load raw data into motherduck base"
```

### タスク6: dbt projectとView marts

**対象ファイル:**
- 作成: `dbt/dbt_project.yml`
- 作成: `dbt/profiles.yml`
- 作成: `dbt/models/sources.yml`
- 作成: `dbt/models/marts/daily_health.sql`
- 作成: `dbt/models/marts/sleep_analysis.sql`
- 作成: `dbt/models/marts/schema.yml`

**インターフェース:**
- 提供: `marts.daily_health` View
- 提供: `marts.sleep_analysis` View

- [ ] **ステップ1: MotherDuck dbt profileを設定する**

`dbt/profiles.yml`:

```yaml
personal_data_platform:
  target: prod
  outputs:
    prod:
      type: duckdb
      path: "md:personal_data?motherduck_token={{ env_var('MOTHERDUCK_TOKEN') }}"
      threads: 2
```

- [ ] **ステップ2: project defaultをViewにする**

`dbt_project.yml`:

```yaml
name: personal_data_platform
version: "1.0"
config-version: 2
profile: personal_data_platform
model-paths: ["models"]

models:
  personal_data_platform:
    marts:
      +schema: marts
      +materialized: view
```

- [ ] **ステップ3: base sourceを定義する**

3つのGoogle Health base tableと、想定columnのtestを定義する。

- [ ] **ステップ4: `daily_health`を作成する**

初期martでは、正しくmodel化済みのfactだけを集約する:

```text
date
steps
average_heart_rate
sleep_minutes
```

date groupingでは`Asia/Tokyo`へのtimezone変換を明示する。

- [ ] **ステップ5: `sleep_analysis`を作成する**

sleep sessionごとに1行として以下を公開する:

```text
sleep_start
sleep_end
sleep_minutes
minutes_awake
source/platform fields needed for analysis
```

- [ ] **ステップ6: dbt testを追加する**

At minimum:

```text
date not_null
sleep_end > sleep_start
beats_per_minute > 0 at base/source-test level where supported
step_count >= 0
```

- [ ] **ステップ7: Run dbt**

```bash
cd dbt
dbt debug
dbt run
dbt test
```

`information_schema.tables.table_type`でmart modelが`VIEW`になっていることを確認する。

- [ ] **ステップ8: Commit**

```bash
git add dbt
git commit -m "feat: add motherduck analytical views"
```

### タスク7: dbt-runner Jobとdeploy時のみの実行

**対象ファイル:**
- 変更: `src/personal_data_platform/entrypoint.py`
- 作成: `src/personal_data_platform/dbt_runner.py`
- 変更: `.github/workflows/deploy.yml`
- テスト: `tests/unit/test_dbt_runner.py`

**インターフェース:**
- 提供: `run_dbt() -> int`
- 制約: no ingestion-triggered dbt execution

- [ ] **ステップ1: runnerを実装する**

Execute:

```bash
dbt run --project-dir /app/dbt --profiles-dir /app/dbt
dbt test --project-dir /app/dbt --profiles-dir /app/dbt
```

どちらかが失敗した場合はnon-zeroを返す。

- [ ] **ステップ2: dbt directoryをDocker imageへcopyする**

Dockerfileを更新する:

```dockerfile
COPY dbt /app/dbt
RUN pip install --no-cache-dir . dbt-duckdb
```

- [ ] **ステップ3: dbt/schema file変更時だけdbt Jobを起動する**

deploy workflowで以下の変更を検知する:

```text
dbt/**
sql/base/**
```

Cloud Run deploy後に`dbt-runner`を実行する。

通常のLoader成功後にはdbtを呼び出さない。

- [ ] **ステップ4: Verify**

dbt codeを変更せずbase dataを更新し、dbt Jobが作成されないことを確認する。その後Viewをqueryし、新しいbase状態が反映されていることを確認する。

- [ ] **ステップ5: commitする**

```bash
git add Dockerfile src/personal_data_platform/dbt_runner.py \
  src/personal_data_platform/entrypoint.py .github/workflows/deploy.yml tests
git commit -m "feat: run dbt only for analytical definition changes"
```

## 計画全体の検証

実行:

```bash
pytest tests/unit tests/integration -v
ruff check src tests
cd dbt && dbt run && dbt test
```

続けて、制御した訂正データで検証する:

```text
Raw v1 → base BPM 72 → mart shows 72
Raw v2 same stable key BPM 74 → base row count stays 1 → mart shows 74
```

martsはViewなので、2回のRaw load間でdbt実行が発生してはならない。
