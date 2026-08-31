import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def _workflow_step_script(relative_path: str, step_name: str) -> str:
    workflow = _read(relative_path)
    step = workflow.split(f"      - name: {step_name}\n", 1)[1]
    step = step.split("\n      - name:", 1)[0]
    return textwrap.dedent(step.split("        run: |\n", 1)[1])


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
    assert "github.ref == 'refs/heads/main'" in deploy
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


def test_cloud_workflows_wait_for_bootstrap_configuration() -> None:
    plan = _read(".github/workflows/terraform-plan.yml")
    deploy = _read(".github/workflows/terraform-deploy.yml")

    assert "${{ vars.GCP_PLAN_WIF_PROVIDER != '' &&" in plan
    assert "github.event.pull_request.head.repo.full_name == github.repository" in plan
    assert "${{ vars.GCP_DEPLOY_WIF_PROVIDER != '' && github.ref == 'refs/heads/main' }}" in deploy


@pytest.mark.parametrize("account", ["github_plan", "github_deploy"])
def test_bootstrap_service_accounts_wait_for_api_enablement(account: str) -> None:
    identity = _read("infra/bootstrap/identity.tf")
    resource = identity.split(f'resource "google_service_account" "{account}" {{', 1)[1]
    resource = resource.split("\n}", 1)[0]

    assert "depends_on = [google_project_service.bootstrap]" in resource


def test_only_preflight_can_override_the_raw_prefix() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    variables = _read("infra/terraform/variables.tf")

    assert jobs.count("B2_RAW_PREFIX") == 1
    assert "B2_RAW_PREFIX                 = var.preflight_b2_prefix" in jobs
    assert 'variable "b2_raw_prefix"' not in variables


def test_analytics_day_boundaries_are_not_advertised_as_configurable() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    variables = _read("infra/terraform/variables.tf")

    assert "ANALYTICS_TIME_ZONE" not in jobs
    assert 'variable "analytics_time_zone"' not in variables
    assert 'variable "scheduler_time_zone"' in variables


@pytest.mark.parametrize(
    ("current_image", "fallback_image", "expected_image"),
    [
        ("runtime@sha256:" + "a" * 64, "runtime@sha256:" + "b" * 64, "runtime@sha256:" + "a" * 64),
        ("", "runtime@sha256:" + "b" * 64, "runtime@sha256:" + "b" * 64),
        ("", "runtime:latest", None),
    ],
)
def test_plan_resolves_the_v1_job_image_before_using_a_fallback(
    tmp_path: Path,
    current_image: str,
    fallback_image: str,
    expected_image: str | None,
) -> None:
    script = _workflow_step_script(
        ".github/workflows/terraform-plan.yml", "Resolve current runtime image"
    )
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(
        "#!/bin/bash\n"
        'for argument in "$@"; do\n'
        '  if [[ "${argument}" == '
        "'--format=value(spec.template.spec.template.spec.containers[0].image)' ]]; then\n"
        "    printf '%s\\n' \"${CURRENT_IMAGE}\"\n"
        "    exit 0\n"
        "  fi\n"
        "done\n"
        "exit 1\n",
        encoding="utf-8",
    )
    gcloud.chmod(0o755)
    github_env = tmp_path / "github-env"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "CURRENT_IMAGE": current_image,
        "GITHUB_ENV": str(github_env),
        "TF_VAR_project_id": "example-project",
        "TF_VAR_region": "asia-northeast1",
        "TF_VAR_image_uri": fallback_image,
    }

    result = subprocess.run(
        ["bash", "-c", script],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    if expected_image is None:
        assert result.returncode != 0
        assert not github_env.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert github_env.read_text(encoding="utf-8") == f"TF_VAR_image_uri={expected_image}\n"


@pytest.mark.parametrize(
    ("job", "actions", "expected"),
    [
        ("dbt", ["create"], "true"),
        ("dbt", ["delete", "create"], "true"),
        ("dbt", ["update"], "false"),
        ("dbt", ["no-op"], "false"),
        ("loader", ["create"], "false"),
        (None, [], "false"),
    ],
)
def test_deploy_detects_initial_dbt_job_before_apply(
    tmp_path: Path, job: str | None, actions: list[str], expected: str
) -> None:
    script = _workflow_step_script(".github/workflows/terraform-deploy.yml", "Plan runtime changes")
    terraform = tmp_path / "terraform"
    terraform.write_text(
        "#!/bin/bash\n"
        'if [[ "$2" == "plan" ]]; then exit 0; fi\n'
        'if [[ "$2" == "show" ]]; then printf \'%s\\n\' "$PLAN_JSON"; exit 0; fi\n'
        "exit 1\n",
        encoding="utf-8",
    )
    terraform.chmod(0o755)
    changes = (
        [{"address": f'google_cloud_run_v2_job.runtime["{job}"]', "change": {"actions": actions}}]
        if job
        else []
    )
    github_output = tmp_path / "github-output"

    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "PLAN_JSON": json.dumps({"resource_changes": changes}),
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert github_output.read_text(encoding="utf-8") == f"first_deployment={expected}\n"


@pytest.mark.parametrize(
    ("first_deployment", "requested", "event", "changed_path", "expected"),
    [
        ("true", "false", "push", "infra/terraform/jobs.tf", "true"),
        ("false", "true", "workflow_dispatch", "", "true"),
        ("false", "false", "workflow_dispatch", "", "false"),
        ("false", "false", "push", "dbt/models/screen_time.sql", "true"),
        ("false", "false", "push", "src/personal_data_platform/migrations/002_new.sql", "true"),
        ("false", "false", "push", "src/personal_data_platform/cli.py", "false"),
        ("false", "false", "push", "infra/terraform/jobs.tf", "false"),
        ("false", "false", "push", "", "false"),
    ],
)
def test_deploy_runs_dbt_only_when_initial_requested_or_models_changed(
    tmp_path: Path,
    first_deployment: str,
    requested: str,
    event: str,
    changed_path: str,
    expected: str,
) -> None:
    script = _workflow_step_script(
        ".github/workflows/terraform-deploy.yml", "Determine whether to run dbt"
    )
    git = tmp_path / "git"
    git.write_text(
        "#!/bin/bash\n"
        'for argument in "$@"; do\n'
        '  if [[ "$argument" == */ && "$CHANGED_PATH" == "$argument"* ]]; then exit 1; fi\n'
        "done\n"
        "exit 0\n",
        encoding="utf-8",
    )
    git.chmod(0o755)
    github_output = tmp_path / "github-output"

    result = subprocess.run(
        ["bash", "-c", script],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "FIRST_DEPLOYMENT": first_deployment,
            "REQUESTED": requested,
            "GITHUB_EVENT_NAME": event,
            "BEFORE_SHA": "before",
            "GITHUB_SHA": "after",
            "CHANGED_PATH": changed_path,
            "GITHUB_OUTPUT": str(github_output),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert github_output.read_text(encoding="utf-8") == f"run={expected}\n"


def test_deploy_initializes_models_only_after_apply_and_preflight() -> None:
    workflow = _read(".github/workflows/terraform-deploy.yml")

    assert workflow.index("name: Plan runtime changes") < workflow.index(
        "name: Apply runtime changes"
    )
    assert workflow.index("name: Apply runtime changes") < workflow.index(
        "name: Run isolated deployment preflight"
    )
    assert workflow.index("name: Run isolated deployment preflight") < workflow.index(
        "name: Initialize or update dbt models"
    )
    assert "FIRST_DEPLOYMENT: ${{ steps.plan.outputs.first_deployment }}" in workflow
    assert "if: ${{ steps.dbt.outputs.run == 'true' }}" in workflow
