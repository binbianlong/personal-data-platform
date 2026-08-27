from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_uses_only_approved_jobs_and_schedules() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    scheduler = _read("infra/terraform/scheduler.tf")

    for role in ("preflight", "loader", "dbt", "reconciliation"):
        assert f"    {role} = {{" in jobs
    assert "cloudtasks" not in (jobs + scheduler).lower()
    assert "webhook" not in (jobs + scheduler).lower()
    assert "fetch" not in (jobs + scheduler).lower()
    assert 'default     = "15 * * * *"' in _read("infra/terraform/variables.tf")
    assert 'default     = "30 4 * * *"' in _read("infra/terraform/variables.tf")
    assert 'default     = "Asia/Tokyo"' in _read("infra/terraform/variables.tf")
    assert "PREFLIGHT_MOTHERDUCK_DATABASE = var.preflight_motherduck_database" in jobs
    assert 'RECONCILIATION_HEARTBEAT_URL = "healthchecks_ping_url"' in jobs


def test_preflight_credentials_are_separate_from_production() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    secrets = _read("infra/terraform/secrets.tf")

    assert 'B2_KEY_ID          = "preflight_b2_key_id"' in jobs
    assert 'B2_APPLICATION_KEY = "preflight_b2_application_key"' in jobs
    assert 'MOTHERDUCK_TOKEN   = "motherduck_preflight_token"' in jobs
    assert 'secret_id = "preflight-b2-key-id"' in secrets
    assert 'secret_id = "motherduck-preflight-token"' in secrets


def test_runtime_image_requires_digest() -> None:
    variables = _read("infra/terraform/variables.tf")
    jobs = _read("infra/terraform/jobs.tf")

    assert "@sha256:[0-9a-f]{64}$" in variables
    assert "image = var.image_uri" in jobs
    assert ":latest" not in jobs


def test_deploy_identity_and_workflow_are_restricted() -> None:
    identity = _read("infra/bootstrap/identity.tf")
    deploy = _read(".github/workflows/terraform-deploy.yml")

    assert "assertion.repository ==" in identity
    assert "assertion.ref == 'refs/heads/main'" in identity
    assert "assertion.workflow_ref ==" in identity
    bootstrap = _read("infra/bootstrap/main.tf")
    assert "roles/resourcemanager.projectIamAdmin" not in bootstrap
    assert "roles/iam.serviceAccountUser" not in bootstrap
    assert "if: ${{ github.ref == 'refs/heads/main' }}" in deploy
    assert "TF_VAR_image_uri=${image_uri}" in deploy


def test_plan_identity_is_separate_and_read_only() -> None:
    identity = _read("infra/bootstrap/identity.tf")
    main = _read("infra/bootstrap/main.tf")

    assert 'account_id   = "github-tf-plan"' in identity
    assert 'account_id   = "github-tf-deploy"' in identity
    assert 'role   = "roles/storage.objectViewer"' in identity
    assert 'role   = "roles/storage.objectAdmin"' in identity
    assert "roles/viewer" in main
    assert "roles/iam.securityReviewer" in main
    assert "deployer_act_as_runtime" in _read("infra/terraform/jobs.tf")
    assert "deployer_act_as_scheduler" in _read("infra/terraform/scheduler.tf")
