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
    assert "TF_VAR_runtime_image_uri=${current_image}" in deploy


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
    preflight = storage.split('resource "google_storage_bucket" "preflight" {', 1)[1]
    preflight = preflight.split('resource "google_service_account" "processing" {', 1)[0]

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
def test_runtime_workflows_require_numeric_pins_before_cloud_work(
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
                    "ARTIFACT_REPOSITORY",
                    "GCP_PROJECT_ID",
                    "GCP_REGION",
                    "IMAGE_NAME",
                    "GCP_PLAN_SERVICE_ACCOUNT",
                    "TF_STATE_BUCKET",
                    "TF_VAR_alert_email",
                    "TF_VAR_rebuild_impersonator_member",
                    "TF_VAR_deployer_service_account_email",
                    "TF_VAR_project_id",
                ],
                "example",
            ),
            "TF_VAR_secret_versions": json.dumps(versions),
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
                ".github/workflows/terraform-deploy.yml", "Prepare runtime image"
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
            "GCP_REGION": "us-west1",
            "ARTIFACT_REPOSITORY": "runtime",
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
    assert output.read_text() == f"TF_VAR_runtime_image_uri={expected_image}\n"
    if event == "workflow_dispatch":
        assert docker_log.read_text().splitlines() == [
            f"build --tag {IMAGE_PATH}:{sha} .",
            f"push {IMAGE_PATH}:{sha}",
        ]
    else:
        assert not docker_log.exists()


@pytest.mark.parametrize("fallback_region", ["us-central1", "us-west1"])
def test_plan_fallback_requires_runtime_region(tmp_path: Path, fallback_region: str) -> None:
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
            _workflow_step_script(".github/workflows/terraform-plan.yml", "Resolve runtime image"),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TF_VAR_project_id": "example-project",
            "GCP_REGION": "us-west1",
            "ARTIFACT_REPOSITORY": "runtime",
            "TF_VAR_runtime_image_uri": fallback,
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
        assert output.read_text() == f"TF_VAR_runtime_image_uri={fallback}\n"
