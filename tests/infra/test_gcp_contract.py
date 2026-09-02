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


def test_preflight_uses_an_isolated_gcs_bucket_and_token() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    secrets = _read("infra/terraform/secrets.tf")

    assert "GCS_BUCKET                    = local.raw_bucket_name" in jobs
    assert "GCS_PREFLIGHT_BUCKET          = local.preflight_bucket_name" in jobs
    assert 'MOTHERDUCK_TOKEN = "motherduck_preflight_token"' in jobs
    assert 'secret_id = "motherduck-preflight-token"' in secrets
    assert "B2_KEY_ID" not in jobs
    assert "B2_APPLICATION_KEY" not in jobs


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
    assert "roles/iam.securityReviewer" not in main
    assert "deployer_act_as_runtime" in _read("infra/terraform/jobs.tf")
    assert "deployer_act_as_scheduler" in _read("infra/terraform/scheduler.tf")


def test_runtime_bucket_manager_has_no_object_data_permissions() -> None:
    roles = _read("infra/bootstrap/storage_roles.tf")
    identity = _read("infra/bootstrap/identity.tf")

    bucket_manager = roles.split(
        'resource "google_project_iam_custom_role" "runtime_bucket_manager" {', 1
    )[1]
    assert '"storage.buckets.getIamPolicy"' in bucket_manager
    assert '"storage.buckets.setIamPolicy"' in bucket_manager
    assert '"storage.objects.get"' not in bucket_manager
    assert "github_deploy_bucket_manager" in identity


def test_plan_identity_can_refresh_bucket_metadata_without_object_access() -> None:
    roles = _read("infra/bootstrap/storage_roles.tf")
    identity = _read("infra/bootstrap/identity.tf")

    bucket_reader = roles.split(
        'resource "google_project_iam_custom_role" "runtime_bucket_reader" {', 1
    )[1]
    assert '"storage.buckets.get"' in bucket_reader
    assert '"storage.buckets.getIamPolicy"' in bucket_reader
    assert "role    = google_project_iam_custom_role.runtime_bucket_reader.name" in identity
    assert 'member  = "serviceAccount:${google_service_account.github_plan.email}"' in identity
    assert '"storage.objects.get"' not in bucket_reader
    assert '"storage.objects.list"' not in bucket_reader


def test_cloud_workflows_wait_for_bootstrap_configuration() -> None:
    plan = _read(".github/workflows/terraform-plan.yml")
    deploy = _read(".github/workflows/terraform-deploy.yml")

    assert "${{ vars.GCP_PLAN_WIF_PROVIDER != '' &&" in plan
    assert "github.event.pull_request.head.repo.full_name == github.repository" in plan
    assert "GCP_PLAN_SERVICE_ACCOUNT: ${{ vars.GCP_PLAN_SERVICE_ACCOUNT }}" in plan
    assert "for name in GCP_PLAN_SERVICE_ACCOUNT TF_STATE_BUCKET" in plan
    assert "${{ vars.GCP_DEPLOY_WIF_PROVIDER != '' && github.ref == 'refs/heads/main' }}" in deploy


@pytest.mark.parametrize("account", ["github_plan", "github_deploy"])
def test_bootstrap_service_accounts_wait_for_api_enablement(account: str) -> None:
    identity = _read("infra/bootstrap/identity.tf")
    resource = identity.split(f'resource "google_service_account" "{account}" {{', 1)[1]
    resource = resource.split("\n}", 1)[0]

    assert "depends_on = [google_project_service.bootstrap]" in resource


def test_runtime_uses_fixed_gcs_buckets_without_a_prefix_override() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    variables = _read("infra/terraform/variables.tf")

    assert jobs.count("GCS_PREFLIGHT_BUCKET") == 1
    assert jobs.count("GCS_BUCKET") == 3
    assert "B2_RAW_PREFIX" not in jobs
    assert 'variable "preflight_b2_prefix"' not in variables


def test_raw_bucket_is_standard_and_permanently_deletes_segments_after_90_days() -> None:
    storage = _read("infra/terraform/storage.tf")
    raw = storage.split('resource "google_storage_bucket" "raw" {', 1)[1]
    raw = raw.split('resource "google_storage_bucket" "preflight" {', 1)[0]

    assert 'raw_bucket_name       = "${var.project_id}-pdp-raw"' in storage
    assert "location                    = var.region" in raw
    assert 'storage_class               = "STANDARD"' in raw
    assert "force_destroy               = false" in raw
    assert "uniform_bucket_level_access = true" in raw
    assert 'public_access_prevention    = "enforced"' in raw
    assert 'type = "Delete"' in raw
    assert "age            = 90" in raw
    assert "matches_prefix = [local.raw_object_prefix]" in raw
    assert 'matches_suffix = [".segb.gz"]' in raw
    assert "retention_duration_seconds = 0" in raw
    assert "prevent_destroy = true" in raw
    assert "versioning" not in raw
    assert "autoclass" not in raw


def test_preflight_bucket_isolated_and_orphans_expire_after_one_day() -> None:
    storage = _read("infra/terraform/storage.tf")
    preflight = storage.split('resource "google_storage_bucket" "preflight" {', 1)[1]
    preflight = preflight.split('data "google_iam_policy" "raw_bucket" {', 1)[0]

    assert 'preflight_bucket_name = "${var.project_id}-pdp-preflight"' in storage
    assert 'storage_class               = "STANDARD"' in preflight
    assert 'type = "Delete"' in preflight
    assert "age            = 1" in preflight
    assert 'matches_prefix = ["test/preflight/"]' in preflight
    assert "retention_duration_seconds = 0" in preflight


def test_bucket_iam_is_authoritative_and_service_specific() -> None:
    storage = _read("infra/terraform/storage.tf")
    roles = _read("infra/bootstrap/storage_roles.tf")

    assert 'resource "google_storage_bucket_iam_policy" "raw"' in storage
    assert 'resource "google_storage_bucket_iam_policy" "preflight"' in storage
    assert "google_storage_bucket_iam_member" not in storage
    assert 'permissions = ["storage.objects.create"]' in roles
    assert 'role = "roles/storage.objectViewer"' in storage
    assert 'google_service_account.runtime["loader"]' in storage
    assert 'google_service_account.runtime["reconciliation"]' in storage
    assert "google_service_account.rebuild_operator" in storage
    assert "collector_raw_create_only" in storage
    assert "collector_control_state_only" in storage
    assert "device_manifest_key" in storage
    assert "projectViewer:" not in storage
    assert "projectEditor:" not in storage


def test_rebuild_uses_a_separate_read_only_impersonated_identity() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    storage = _read("infra/terraform/storage.tf")

    assert 'account_id   = "raw-rebuild-operator"' in jobs
    assert 'resource "google_service_account_iam_member" "rebuild_operator_impersonator"' in jobs
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in jobs
    assert "google_service_account.rebuild_operator.email" in storage
    assert 'role = "roles/storage.objectViewer"' in storage


def test_former_b2_secrets_are_retained_but_not_injected() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    secrets = _read("infra/terraform/secrets.tf")
    outputs = _read("infra/terraform/outputs.tf")

    assert secrets.count("\nmoved {\n") == 6
    assert secrets.count("\nremoved {\n") == 6
    assert secrets.count("destroy = false") == 6
    assert "B2_" not in jobs
    assert "for key in keys(local.runtime_secrets)" in outputs


def test_runtime_and_artifact_registry_are_fixed_to_us_central1() -> None:
    runtime_variables = _read("infra/terraform/variables.tf")
    bootstrap_variables = _read("infra/bootstrap/variables.tf")
    plan = _read(".github/workflows/terraform-plan.yml")
    deploy = _read(".github/workflows/terraform-deploy.yml")

    assert 'default     = "us-central1"' in runtime_variables
    assert 'default     = "us-central1"' in bootstrap_variables
    assert "TF_VAR_region: us-central1" in plan
    assert "GCP_REGION: us-central1" in deploy
    assert "TF_VAR_region: us-central1" in deploy
    assert "GCP_COLLECTOR_IMPERSONATOR_MEMBER" in plan
    assert "GCP_COLLECTOR_IMPERSONATOR_MEMBER" in deploy


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
        "TF_VAR_region": "us-central1",
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
