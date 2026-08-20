# 基盤・Infrastructure 実装計画

> **エージェント実装時の必須事項:** この計画をタスク単位で実装する際は、`superpowers:subagent-driven-development`（推奨）または `superpowers:executing-plans` を使用する。進捗管理にはチェックボックス（`- [ ]`）を使用する。

**目標:** `personal-data-platform` のPython実行基盤、共通Docker image、Terraform、GitHub Actions、Workload Identity Federationを構築し、後続のingestion/analytics/reconciliationを載せられるGCP基盤を作る。

**アーキテクチャ:** 1 repository・1 Python project・1 Docker imageを共有し、Cloud Run Service/Jobを役割別に展開する。GCP resourceはTerraformで管理し、Terraform stateは専用GCS bucketへ保存する。GitHub ActionsからGCPへの認証はWorkload Identity Federationのみを使用する。

**技術スタック:** Python, pytest, Ruff, Docker, Terraform, Google Cloud Run, Cloud Tasks, Cloud Scheduler, Secret Manager, Artifact Registry, GitHub Actions, Workload Identity Federation

**設計spec:** `docs/superpowers/specs/2026-08-20-personal-data-platform-design.md`

## 全体制約

- Raw System of RecordはBackblaze B2であり、GCSへRawを保存しない。
- Analytics DBはMotherDuck。
- 初期UI / Webアプリは作らない。
- Pub/Sub、Firestore、PostgreSQL orchestration DB、永続Parquet、Apache Icebergを初期導入しない。
- Cloud Runは`health-webhook`、`health-fetch`、`health-loader`をServiceとして、`dbt-runner`、`reconciliation`をJobとして分離する。
- Secret値そのものはTerraform stateへ保存しない。TerraformはSecret Manager resourceだけ作り、値投入は手動または専用CI stepで行う。
- GitHub Actionsへ長期GCP Service Account Keyを保存しない。
- martsがViewの間はデータ更新ごとのdbt Jobを起動しない。

---

## ファイル構成

```text
personal-data-platform/
├─ pyproject.toml
├─ Dockerfile
├─ .dockerignore
├─ .gitignore
├─ src/personal_data_platform/
│  ├─ __init__.py
│  ├─ entrypoint.py
├─ tests/unit/
│  └─ test_entrypoint.py
├─ infra/
│  ├─ bootstrap/
│  │  ├─ main.tf
│  │  ├─ variables.tf
│  │  └─ outputs.tf
│  └─ terraform/
│     ├─ backend.tf
│     ├─ providers.tf
│     ├─ variables.tf
│     ├─ APIs.tf
│     ├─ artifact_registry.tf
│     ├─ service_accounts.tf
│     ├─ secrets.tf
│     ├─ cloud_tasks.tf
│     ├─ cloud_run.tf
│     ├─ scheduler.tf
│     └─ outputs.tf
└─ .github/workflows/
   ├─ ci.yml
   ├─ terraform-plan.yml
   └─ deploy.yml
```

### タスク1: Pythonプロジェクトの雛形とrole entrypoint

**対象ファイル:**
- 作成: `pyproject.toml`
- 作成: `.gitignore`
- 作成: `src/personal_data_platform/__init__.py`
- 作成: `src/personal_data_platform/entrypoint.py`
- テスト: `tests/unit/test_entrypoint.py`

**インターフェース:**
- 提供: `personal_data_platform.entrypoint.main(argv: list[str] | None = None) -> int`
- 提供: runtime role names `webhook`, `fetch`, `loader`, `dbt`, `reconciliation`

- [ ] **ステップ1: role dispatchの失敗テストを書く**

```python
from personal_data_platform.entrypoint import resolve_role

def test_resolve_role_accepts_known_roles():
    assert resolve_role("webhook") == "webhook"
    assert resolve_role("fetch") == "fetch"
    assert resolve_role("loader") == "loader"
    assert resolve_role("dbt") == "dbt"
    assert resolve_role("reconciliation") == "reconciliation"
```

- [ ] **ステップ2: テストを実行して失敗を確認する**

実行:

```bash
pytest tests/unit/test_entrypoint.py -v
```

期待結果: import or `resolve_role` failure.

- [ ] **ステップ3: 最小限のproject metadataとdispatcherを作る**

`pyproject.toml`:

```toml
[project]
name = "personal-data-platform"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = [
  "fastapi",
  "uvicorn[standard]",
  "pydantic-settings",
  "httpx",
  "google-cloud-tasks",
  "google-auth",
  "tink",
  "boto3>=1.28.0",
  "duckdb",
]

[project.optional-dependencies]
dev = [
  "pytest",
  "pytest-asyncio",
  "respx",
  "ruff",
]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]

[tool.ruff]
line-length = 100
target-version = "py313"
```

`src/personal_data_platform/entrypoint.py`:

```python
import argparse

ROLES = {"webhook", "fetch", "loader", "dbt", "reconciliation"}

def resolve_role(role: str) -> str:
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    return role

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=sorted(ROLES))
    args = parser.parse_args(argv)
    resolve_role(args.role)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
```

`.gitignore`:

```text
.venv/
__pycache__/
*.py[cod]
.pytest_cache/
.ruff_cache/
.env
.terraform/
*.tfstate
*.tfstate.*
.DS_Store
```

- [ ] **ステップ4: Unit Testとlintを実行する**

```bash
pytest tests/unit/test_entrypoint.py -v
ruff check src tests
```

期待結果: PASS.

- [ ] **ステップ5: commitする**

```bash
git add pyproject.toml .gitignore src tests
git commit -m "chore: scaffold personal data platform"
```

### タスク2: 共通Docker image

**対象ファイル:**
- 作成: `Dockerfile`
- 作成: `.dockerignore`
- テスト: `tests/unit/test_container_contract.py`

**インターフェース:**
- 使用: `python -m personal_data_platform.entrypoint <role>`
- 提供: one deployable image usable by all Cloud Run roles

- [ ] **ステップ1: Container contract testを書く**

```python
from pathlib import Path

def test_dockerfile_uses_single_python_entrypoint():
    text = Path("Dockerfile").read_text()
    assert "python" in text
    assert "personal_data_platform.entrypoint" in text
```

- [ ] **ステップ2: 失敗を確認する**

```bash
pytest tests/unit/test_container_contract.py -v
```

期待結果: FAIL because `Dockerfile` does not exist.

- [ ] **ステップ3: Dockerfileを作成する**

```dockerfile
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml /app/
COPY src /app/src

RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "personal_data_platform.entrypoint"]
```

`.dockerignore`:

```text
.git
.github
.venv
__pycache__
.pytest_cache
.ruff_cache
tests
infra
docs
```

- [ ] **ステップ4: テストとbuildを実行する**

```bash
pytest tests/unit/test_container_contract.py -v
docker build -t personal-data-platform:test .
docker run --rm personal-data-platform:test webhook
```

期待結果: exit code 0.

- [ ] **ステップ5: commitする**

```bash
git add Dockerfile .dockerignore tests/unit/test_container_contract.py
git commit -m "build: add shared runtime image"
```

### タスク3: Terraform stateとGitHub連携用bootstrap

**対象ファイル:**
- 作成: `infra/bootstrap/main.tf`
- 作成: `infra/bootstrap/variables.tf`
- 作成: `infra/bootstrap/outputs.tf`

**インターフェース:**
- 提供: GCS state bucket
- 提供: Workload Identity Pool + GitHub OIDC provider
- 提供: CI deploy service account
- 提供: exact backend bucket name and WIF provider resource name

- [ ] **ステップ1: bootstrap用variablesを作成する**

`infra/bootstrap/variables.tf`:

```hcl
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-northeast1"
}
variable "github_owner" { type = string }
variable "github_repository" {
  type    = string
  default = "personal-data-platform"
}
variable "state_bucket_name" { type = string }
```

- [ ] **ステップ2: bootstrap resourceを作成する**

`infra/bootstrap/main.tf`:

```hcl
terraform {
  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_storage_bucket" "terraform_state" {
  name                        = var.state_bucket_name
  location                    = "ASIA"
  uniform_bucket_level_access = true
  versioning { enabled = true }
}

resource "google_iam_workload_identity_pool" "github" {
  workload_identity_pool_id = "github-actions"
  display_name              = "GitHub Actions"
}

resource "google_iam_workload_identity_pool_provider" "github" {
  workload_identity_pool_id          = google_iam_workload_identity_pool.github.workload_identity_pool_id
  workload_identity_pool_provider_id = "github"
  display_name                       = "GitHub"
  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
  }
  attribute_condition = "assertion.repository == '${var.github_owner}/${var.github_repository}'"
  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "github_deploy" {
  account_id   = "github-deploy"
  display_name = "GitHub deployment"
}

resource "google_service_account_iam_member" "github_wif" {
  service_account_id = google_service_account.github_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github.name}/attribute.repository/${var.github_owner}/${var.github_repository}"
}
```

- [ ] **ステップ3: outputsを追加する**

```hcl
output "state_bucket_name" {
  value = google_storage_bucket.terraform_state.name
}

output "workload_identity_provider" {
  value = google_iam_workload_identity_pool_provider.github.name
}

output "deploy_service_account" {
  value = google_service_account.github_deploy.email
}
```

- [ ] **ステップ4: bootstrapをvalidateする**

```bash
terraform -chdir=infra/bootstrap init
terraform -chdir=infra/bootstrap fmt -check
terraform -chdir=infra/bootstrap validate
```

期待結果: all commands succeed.

- [ ] **ステップ5: 管理者identityでbootstrapを一度だけapplyする**

```bash
terraform -chdir=infra/bootstrap apply
```

3つのoutputはGitHub repositoryのSecretsではなくVariablesへ登録する。

- [ ] **ステップ6: commitする**

```bash
git add infra/bootstrap
git commit -m "infra: add terraform bootstrap"
```

### タスク4: Terraformで管理するGCP runtime

**対象ファイル:**
- 作成: `infra/terraform/backend.tf`
- 作成: `infra/terraform/providers.tf`
- 作成: `infra/terraform/variables.tf`
- 作成: `infra/terraform/APIs.tf`
- 作成: `infra/terraform/artifact_registry.tf`
- 作成: `infra/terraform/service_accounts.tf`
- 作成: `infra/terraform/secrets.tf`
- 作成: `infra/terraform/cloud_tasks.tf`
- 作成: `infra/terraform/cloud_run.tf`
- 作成: `infra/terraform/scheduler.tf`
- 作成: `infra/terraform/outputs.tf`

**インターフェース:**
- 提供: `health-webhook`, `health-fetch`, `health-loader`
- 提供: `dbt-runner`, `reconciliation`
- 提供: Cloud Tasks queues `google-health-fetch`, `raw-loader`
- 提供: Secret Manager resource names without secret payloads

- [ ] **ステップ1: provider/backendを設定する**

`terraform init`時にbucketを指定できるよう、partial backendを使用する:

```hcl
terraform {
  backend "gcs" {}

  required_providers {
    google = {
      source = "hashicorp/google"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}
```

- [ ] **ステップ2: runtime variablesを明示的に定義する**

```hcl
variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "asia-northeast1"
}
variable "image_uri" { type = string }
variable "reconciliation_schedule" {
  type    = string
  default = "0 3 * * *"
}
variable "reconciliation_time_zone" {
  type    = string
  default = "Asia/Tokyo"
}
```

- [ ] **ステップ3: 必要なAPIを有効化しArtifact Registryを作成する**

Enable:

```text
run.googleapis.com
cloudtasks.googleapis.com
cloudscheduler.googleapis.com
secretmanager.googleapis.com
artifactregistry.googleapis.com
monitoring.googleapis.com
logging.googleapis.com
iamcredentials.googleapis.com
```

Docker repository `personal-data-platform`を作成する。

- [ ] **ステップ4: 専用Service Accountを作成する**

Create:

```text
health-webhook
health-fetch
health-loader
dbt-runner
reconciliation
cloud-tasks-invoker
scheduler-invoker
```

service間invokeとSecret参照に必要な最小権限だけ付与する。

- [ ] **ステップ5: 値を含まないSecret Manager resourceを作成する**

以下のresourceを作成する:

```text
google-health-endpoint-authorization
google-health-client-id
google-health-client-secret
google-health-refresh-token
b2-key-id
b2-application-key
b2-endpoint
b2-bucket
motherduck-token
healthchecks-ping-url
```

実際のSecret値を含む`google_secret_manager_secret_version` resourceはTerraformで作成しない。

- [ ] **ステップ6: Cloud Tasks queueを作成する**

`google-health-fetch`のtargetは`health-fetch`とする。

`raw-loader`のtargetは`health-loader`とする。

retry回数には上限を設定する。martsはViewなので、初期段階ではdbt refresh queueを作成しない。

- [ ] **ステップ7: Cloud Run ServiceとJobを作成する**

すべてのruntime resourceで`var.image_uri`を使用する。

Service container arguments:

```text
health-webhook → webhook
health-fetch   → fetch
health-loader  → loader
```

Job container arguments:

```text
dbt-runner      → dbt
reconciliation  → reconciliation
```

- [ ] **ステップ8: Reconciliation用Cloud Scheduler triggerを作成する**

Cloud SchedulerからCloud Run Jobs APIを呼び出す:

```text
POST https://run.googleapis.com/v2/projects/<project>/locations/<region>/jobs/reconciliation:run
```

`scheduler-invoker`を使ってOAuth認証する。

- [ ] **ステップ9: validateする**

```bash
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
```

期待結果: PASS.

- [ ] **ステップ10: commitする**

```bash
git add infra/terraform
git commit -m "infra: define platform runtime"
```

### タスク5: CI・デプロイworkflow

**対象ファイル:**
- 作成: `.github/workflows/ci.yml`
- 作成: `.github/workflows/terraform-plan.yml`
- 作成: `.github/workflows/deploy.yml`

**インターフェース:**
- 使用: repository variable `GCP_PROJECT_ID`, `GCP_REGION`, `GCP_WIF_PROVIDER`, `GCP_DEPLOY_SERVICE_ACCOUNT`, `TF_STATE_BUCKET`
- 提供: PR checkとmain branch deployment

- [ ] **ステップ1: Python CIを追加する**

`ci.yml`では以下を実行する:

```bash
python -m pip install -e '.[dev]'
ruff check src tests
pytest -q
```

- [ ] **ステップ2: Terraform plan workflowを追加する**

permissionsを以下に設定する:

```yaml
permissions:
  contents: read
  id-token: write
```

`google-github-actions/auth`で認証し、以下でGCS backendを初期化する:

```bash
terraform -chdir=infra/terraform init \
  -backend-config="bucket=${TF_STATE_BUCKET}" \
  -backend-config="prefix=personal-data-platform"
```

続けて`fmt -check`、`validate`、`plan`を実行する。

- [ ] **ステップ3: main deployment workflowを追加する**

Order:

```text
authenticate
→ build Docker image
→ push Artifact Registry
→ terraform init
→ terraform apply with exact immutable image URI
```

image tagにはcommit SHAを使用する。

- [ ] **ステップ4: workflow YAMLをローカルで検証する**

```bash
python - <<'PY'
from pathlib import Path
for p in Path(".github/workflows").glob("*.yml"):
    assert p.read_text().strip()
print("workflow files present")
PY
```

期待結果: `workflow files present`.

- [ ] **ステップ5: commitする**

```bash
git add .github/workflows
git commit -m "ci: add keyless gcp deployment"
```

## 計画全体の検証

Ingestion計画へ進む前に以下を検証する:

```bash
pytest -q
ruff check src tests
terraform -chdir=infra/bootstrap validate
terraform -chdir=infra/terraform init -backend=false
terraform -chdir=infra/terraform validate
docker build -t personal-data-platform:test .
```

共通imageを5つのruntime roleへdeployでき、Cloud Tasks queueとReconciliation scheduleが存在し、GitHub Actionsが固定GCP keyなしで認証できれば、この基盤計画は完了とする。
