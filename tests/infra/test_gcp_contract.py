import json
import os
import re
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
    assert "TF_VAR_west_image_uri=${current_image}" in deploy


def test_plan_identity_is_separate_and_read_only() -> None:
    identity = _read("infra/bootstrap/identity.tf")
    main = _read("infra/bootstrap/main.tf")

    assert 'account_id   = "github-tf-plan"' in identity
    assert 'account_id   = "github-tf-deploy"' in identity
    assert 'role   = "roles/storage.objectViewer"' in identity
    assert 'role   = "roles/storage.objectAdmin"' in identity
    assert "roles/viewer" in main
    assert "roles/iam.securityReviewer" not in main
    assert "west_deployer" in _read("infra/terraform/runtime.tf")
    assert "west_scheduler_deployer" in _read("infra/terraform/runtime.tf")


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


def test_preflight_bucket_isolated_and_orphans_expire_after_one_day() -> None:
    storage = _read("infra/terraform/runtime.tf")
    preflight = storage.split('resource "google_storage_bucket" "preflight_west" {', 1)[1]
    preflight = preflight.split('resource "google_service_account" "west" {', 1)[0]

    assert re.search(
        r'preflight_bucket_name\s*= "\$\{var.project_id\}-pdp-preflight-west"', storage
    )
    assert 'storage_class               = "STANDARD"' in preflight
    assert 'type = "Delete"' in preflight
    assert "age            = 1" in preflight
    assert 'matches_prefix = ["test/preflight/"]' in preflight
    assert "retention_duration_seconds = 0" in preflight


def test_rebuild_uses_a_separate_read_only_impersonated_identity() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    storage = _read("infra/terraform/runtime.tf")

    assert 'account_id   = "raw-rebuild-operator"' in jobs
    assert 'resource "google_service_account_iam_member" "rebuild_operator_impersonator"' in jobs
    assert 'role               = "roles/iam.serviceAccountTokenCreator"' in jobs
    assert "google_service_account.rebuild_operator.email" in storage
    assert re.search(r'role\s*= "roles/storage.objectViewer"', storage)


def test_former_b2_secrets_are_retained_but_not_injected() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    secrets = _read("infra/terraform/secrets.tf")
    outputs = _read("infra/terraform/outputs.tf")

    assert secrets.count("\nmoved {\n") == 6
    assert secrets.count("\nremoved {\n") == 6
    assert secrets.count("destroy = false") == 6
    assert "B2_" not in jobs
    assert "for key, secret in google_secret_manager_secret.west" in outputs


def test_analytics_day_boundaries_are_not_advertised_as_configurable() -> None:
    jobs = _read("infra/terraform/jobs.tf")
    variables = _read("infra/terraform/variables.tf")

    assert "ANALYTICS_TIME_ZONE" not in jobs
    assert 'variable "analytics_time_zone"' not in variables
    assert 'variable "scheduler_time_zone"' in variables


IMAGE_PATH = "us-west1-docker.pkg.dev/example-project/runtime/runtime"
CURRENT_DIGEST = f"{IMAGE_PATH}@sha256:" + "a" * 64


def _runtime_job(name: str, image: str) -> dict:
    return {
        "metadata": {"name": name},
        "spec": {"template": {"spec": {"template": {"spec": {"containers": [{"image": image}]}}}}},
    }


def test_runtime_resources_preserve_backups_and_support_pausing() -> None:
    bootstrap = _read("infra/bootstrap/main.tf")
    runtime = _read("infra/terraform/runtime.tf")
    assert 'resource "google_storage_bucket" "terraform_state"' in bootstrap
    assert 'resource "google_storage_bucket" "terraform_state_west"' in bootstrap
    assert 'resource "google_artifact_registry_repository" "runtime_west"' in bootstrap
    assert 'resource "google_storage_bucket" "raw_west"' in runtime
    assert 'resource "google_storage_bucket" "preflight_west"' in runtime
    assert 'resource "google_cloud_run_v2_job" "west"' in runtime
    assert 'resource "google_cloud_scheduler_job" "west"' in runtime
    assert re.search(r"paused\s*= !var.west_schedulers_enabled", runtime)
    assert "days_since_custom_time" not in runtime
    assert "raw/screen_time/" not in runtime
    assert "raw/fitbit/v3/" not in runtime
    assert "retention_duration_seconds = 0" in runtime
    assert "prevent_destroy = true" in runtime


def test_pubsub_and_receiver_access_are_separate() -> None:
    queue = _read("infra/terraform/pubsub.tf")
    runtime = _read("infra/terraform/runtime.tf")
    assert "allowed_persistence_regions = [var.west_region]" in queue
    assert re.search(r"enforce_in_transit\s*= true", queue)
    assert re.search(r'message_retention_duration\s*= "604800s"', queue)
    assert re.search(r"retain_acked_messages\s*= false", queue)
    assert 'ttl = ""' in queue
    assert "roles/pubsub.publisher" in queue
    assert "roles/pubsub.subscriber" not in queue
    assert "push_config" in queue
    receiver = runtime.split('resource "google_cloud_run_v2_service" "west"', 1)[1]
    receiver = receiver.split('resource "google_cloud_run_v2_service_iam_member"', 1)[0]
    assert re.search(r"PDP_FITBIT_WEBHOOK_CONFIG\s*=", receiver)
    assert "PDP_FITBIT_OAUTH_CONFIG" not in receiver
    assert "MOTHERDUCK_TOKEN" not in receiver
    assert "GCS_BUCKET" not in receiver


RUNTIME_SECRET_VERSIONS = {
    "owntracks_config": "19",
    "motherduck_token": "7",
    "motherduck_preflight_token": "3",
    "fitbit_oauth_config": "11",
    "fitbit_webhook_config": "13",
    "heartbeat_config": "17",
}


@pytest.mark.parametrize("workflow", ["terraform-plan.yml", "terraform-deploy.yml"])
@pytest.mark.parametrize(
    ("versions", "accepted"),
    [
        (RUNTIME_SECRET_VERSIONS, True),
        (
            {
                key: value
                for key, value in RUNTIME_SECRET_VERSIONS.items()
                if key != "motherduck_preflight_token"
            },
            True,
        ),
        ({}, False),
        (
            {
                key: value
                for key, value in RUNTIME_SECRET_VERSIONS.items()
                if key != "owntracks_config"
            },
            False,
        ),
        ({"motherduck_token": "7"}, False),
        ({**RUNTIME_SECRET_VERSIONS, "unknown_secret": "19"}, False),
    ]
    + [
        ({**RUNTIME_SECRET_VERSIONS, "motherduck_token": version}, False)
        for version in ("latest", "0", "-1", "1.5", "", "01", " 1", "1\n", 1, None)
    ],
)
def test_west_workflows_require_numeric_pins_before_cloud_work(
    workflow: str, versions: dict[str, object], accepted: bool
) -> None:
    result = subprocess.run(
        [
            "bash",
            "-c",
            _workflow_step_script(f".github/workflows/{workflow}", "Validate repository variables"),
        ],
        env={
            **os.environ,
            **dict.fromkeys(
                [
                    "WEST_ARTIFACT_REPOSITORY",
                    "GCP_PROJECT_ID",
                    "WEST_REGION",
                    "IMAGE_NAME",
                    "GCP_PLAN_SERVICE_ACCOUNT",
                    "TF_STATE_BUCKET",
                    "TF_VAR_alert_email",
                    "TF_VAR_rebuild_impersonator_member",
                    "TF_VAR_deployer_service_account_email",
                    "TF_VAR_project_id",
                    "WEST_REGION",
                ],
                "example",
            ),
            "TF_VAR_west_secret_versions": json.dumps(versions),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted, result.stderr


@pytest.mark.parametrize(
    ("event", "job_list_ok", "expected_image"),
    [
        ("push", True, CURRENT_DIGEST),
        ("workflow_dispatch", True, f"{IMAGE_PATH}@sha256:" + "b" * 64),
        ("push", False, None),
    ],
)
def test_deploy_prepares_image_without_retaining_history(
    tmp_path: Path, event: str, job_list_ok: bool, expected_image: str | None
) -> None:
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(
        "#!/bin/bash\n"
        'if [[ "$1 $2 $3" == "run jobs list" ]]; then\n'
        '  [[ "$JOB_LIST_OK" == "true" ]] || exit 7\n'
        '  printf "%s\\n" "$JOBS_JSON"; exit 0\n'
        "fi\n"
        'if [[ "$1 $2" == "auth configure-docker" ]]; then\n'
        '  [[ "$3" == "us-west1-docker.pkg.dev" ]] || exit 90\n'
        "  exit 0\n"
        "fi\n"
        'if [[ "$1 $2 $3 $4" == "artifacts docker images describe" ]]; then\n'
        '  [[ "$5" == "$IMAGE_PATH:$GITHUB_SHA" ]] || exit 90\n'
        '  printf "%s\\n" "$NEW_DIGEST"; exit 0\n'
        "fi\nexit 90\n"
    )
    gcloud.chmod(0o755)
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/bash\n"
        '[[ "$1" == "build" || "$1" == "push" ]] || exit 90\n'
        'printf "%s\\n" "$*" >> "$DOCKER_LOG"\n'
    )
    docker.chmod(0o755)
    docker_log = tmp_path / "docker-log"
    output = tmp_path / "github-env"
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    result = subprocess.run(
        [
            "bash",
            "-c",
            _workflow_step_script(
                ".github/workflows/terraform-deploy.yml", "Prepare west runtime image"
            ),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "JOBS_JSON": json.dumps([_runtime_job("reconciliation-west", CURRENT_DIGEST)]),
            "JOB_LIST_OK": str(job_list_ok).lower(),
            "NEW_DIGEST": "sha256:" + "b" * 64,
            "IMAGE_PATH": IMAGE_PATH,
            "DOCKER_LOG": str(docker_log),
            "GCP_PROJECT_ID": "example-project",
            "WEST_REGION": "us-west1",
            "WEST_ARTIFACT_REPOSITORY": "runtime",
            "IMAGE_NAME": "runtime",
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": sha,
            "BEFORE_SHA": sha,
            "GITHUB_ENV": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if not job_list_ok:
        assert result.returncode == 7
        assert not output.exists()
        assert not docker_log.exists()
        return
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"TF_VAR_west_image_uri={expected_image}\n"
    if event == "workflow_dispatch":
        assert docker_log.read_text().splitlines() == [
            f"build --tag {IMAGE_PATH}:{sha} .",
            f"push {IMAGE_PATH}:{sha}",
        ]
    else:
        assert not docker_log.exists()


@pytest.mark.parametrize("fallback_region", ["us-central1", "us-west1"])
def test_plan_west_fallback_rejects_legacy_repository(tmp_path: Path, fallback_region: str) -> None:
    gcloud = tmp_path / "gcloud"
    gcloud.write_text("#!/bin/bash\nexit 1\n")
    gcloud.chmod(0o755)
    output = tmp_path / "env"
    fallback = (
        f"{fallback_region}-docker.pkg.dev/example-project/runtime/runtime@sha256:" + "a" * 64
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            _workflow_step_script(
                ".github/workflows/terraform-plan.yml", "Resolve west runtime image"
            ),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TF_VAR_project_id": "example-project",
            "WEST_REGION": "us-west1",
            "WEST_ARTIFACT_REPOSITORY": "runtime",
            "TF_VAR_west_image_uri": fallback,
            "GITHUB_ENV": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if fallback_region == "us-central1":
        assert result.returncode != 0
        assert not output.exists()
    else:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"TF_VAR_west_image_uri={fallback}\n"
