import json
import os
import re
import runpy
import subprocess
import sys
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
NEXT_DIGEST = f"{IMAGE_PATH}@sha256:" + "b" * 64


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
    ("event", "job_list_ok", "current_image_present", "dbt_required", "expected_image"),
    [
        ("push", True, True, False, CURRENT_DIGEST),
        ("push", True, False, False, NEXT_DIGEST),
        ("push", True, True, True, NEXT_DIGEST),
        ("workflow_dispatch", True, True, False, NEXT_DIGEST),
        ("push", False, True, False, None),
    ],
)
def test_deploy_prepares_and_protects_image(
    tmp_path: Path,
    event: str,
    job_list_ok: bool,
    current_image_present: bool,
    dbt_required: bool,
    expected_image: str | None,
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
        '  if [[ "$5" == "$CURRENT_DIGEST" ]]; then [[ "$CURRENT_IMAGE_PRESENT" == "true" ]]; exit; fi\n'
        '  [[ "$5" == "$IMAGE_PATH:$GITHUB_SHA" ]] || exit 90\n'
        '  printf "%s\\n" "$NEW_DIGEST"; exit 0\n'
        "fi\n"
        'if [[ "$1 $2 $3 $4" == "artifacts docker tags add" ]]; then\n'
        '  printf "%s %s\\n" "$5" "$6" > "$TAG_LOG"; exit 0\n'
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
    tag_log = tmp_path / "tag-log"
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
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "JOBS_JSON": json.dumps([_runtime_job("reconciliation-west", CURRENT_DIGEST)]),
            "JOB_LIST_OK": str(job_list_ok).lower(),
            "CURRENT_DIGEST": CURRENT_DIGEST,
            "CURRENT_IMAGE_PRESENT": str(current_image_present).lower(),
            "NEW_DIGEST": "sha256:" + "b" * 64,
            "IMAGE_PATH": IMAGE_PATH,
            "DOCKER_LOG": str(docker_log),
            "TAG_LOG": str(tag_log),
            "GCP_PROJECT_ID": "example-project",
            "GCP_REGION": "us-west1",
            "ARTIFACT_REPOSITORY": "runtime",
            "IMAGE_NAME": "runtime",
            "GITHUB_EVENT_NAME": event,
            "GITHUB_SHA": sha,
            "BEFORE_SHA": sha,
            "GITHUB_ENV": str(output),
            "DBT_DEPLOY_REQUIRED": str(dbt_required).lower(),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if not job_list_ok:
        assert result.returncode == 7
        assert not output.exists()
        assert not docker_log.exists()
        assert not tag_log.exists()
        return
    assert result.returncode == 0, result.stderr
    assert output.read_text() == f"TF_VAR_runtime_image_uri={expected_image}\n"
    assert tag_log.read_text() == (
        f"{expected_image} {IMAGE_PATH}:deployed-live-{expected_image.rsplit(':', 1)[1]}\n"
    )
    if expected_image == NEXT_DIGEST:
        assert docker_log.read_text().splitlines() == [
            f"build --tag {IMAGE_PATH}:{sha} .",
            f"push {IMAGE_PATH}:{sha}",
        ]
    else:
        assert not docker_log.exists()


def test_deploy_retains_images_during_partial_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    main = runpy.run_path(str(ROOT / ".github/scripts/runtime-image-tags.py"))["main"]
    for key, value in {
        "GCP_PROJECT_ID": "example-project",
        "GCP_REGION": "us-west1",
        "ARTIFACT_REPOSITORY": "runtime",
        "IMAGE_NAME": "runtime",
    }.items():
        monkeypatch.setenv(key, value)

    traffic_digest = f"{IMAGE_PATH}@sha256:" + "c" * 64
    daily_image = CURRENT_DIGEST
    tags = {"regular-build-tag": CURRENT_DIGEST}

    def fake_gcloud(*args: str) -> str:
        if args[:3] == ("run", "jobs", "list"):
            return json.dumps([_runtime_job("reconciliation-west", daily_image)])
        if args[:3] == ("run", "services", "list"):
            return json.dumps(
                [
                    {
                        "spec": {"template": {"spec": {"containers": [{"image": NEXT_DIGEST}]}}},
                        "status": {"traffic": [{"revisionName": "older", "percent": 100}]},
                    }
                ]
            )
        if args[:4] == ("run", "revisions", "describe", "older"):
            return json.dumps(
                {
                    "spec": {"containers": [{"image": traffic_digest}]},
                    "status": {"imageDigest": traffic_digest},
                }
            )
        if args[:4] == ("artifacts", "docker", "images", "list"):
            return "\n".join(
                image.split("@")[1] for image in (CURRENT_DIGEST, NEXT_DIGEST, traffic_digest)
            )
        if args[:4] == ("artifacts", "docker", "tags", "list"):
            return json.dumps(
                [
                    {"tag": f"tags/{tag}", "version": f"versions/{image.split('@')[1]}"}
                    for tag, image in tags.items()
                ]
            )
        if args[:4] == ("artifacts", "docker", "tags", "add"):
            tags[args[5].rsplit(":", 1)[1]] = args[4]
            return ""
        if args[:4] == ("artifacts", "docker", "tags", "delete"):
            del tags[args[4].rsplit(":", 1)[1]]
            return ""
        raise AssertionError(args)

    monkeypatch.setitem(main.__globals__, "gcloud", fake_gcloud)
    monkeypatch.setattr(sys, "argv", ["runtime-image-tags.py", "protect"])
    main()
    assert set(tags.values()) == {CURRENT_DIGEST, NEXT_DIGEST, traffic_digest}
    assert tags["deployed-current"] == CURRENT_DIGEST
    assert tags["deployed-live-" + "c" * 64] == traffic_digest

    monkeypatch.setattr(sys, "argv", ["runtime-image-tags.py", "finish", NEXT_DIGEST])
    protected_tags = tags.copy()
    with pytest.raises(RuntimeError, match="daily job does not reference"):
        main()
    assert tags == protected_tags

    daily_image = NEXT_DIGEST
    main()
    assert tags == {
        "regular-build-tag": CURRENT_DIGEST,
        "deployed-current": NEXT_DIGEST,
        "deployed-rollback": CURRENT_DIGEST,
        "deployed-live-" + "b" * 64: NEXT_DIGEST,
        "deployed-live-" + "c" * 64: traffic_digest,
    }
    main()
    assert tags["deployed-rollback"] == CURRENT_DIGEST


@pytest.mark.parametrize(
    ("applied", "force", "required"),
    [
        ("current", False, False),
        ("current", True, True),
        ("previous", False, True),
        ("other-database", False, True),
        ("missing", False, True),
    ],
)
def test_deploy_checks_last_successfully_applied_dbt_definitions(
    tmp_path: Path, applied: str, force: bool, required: bool
) -> None:
    output = tmp_path / "github-env"
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    dbt = subprocess.check_output(["git", "rev-parse", "HEAD:dbt"], cwd=ROOT, text=True).strip()
    versions = {
        "current": f"production {dbt}",
        "previous": "production " + "f" * len(dbt),
        "other-database": f"old_database {dbt}",
        "missing": "",
    }
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(
        "#!/bin/bash\n"
        '[[ "$1 $2 $3" == "storage cat $EXPECTED_MARKER" && -n "$APPLIED" ]] || exit 1\n'
        'printf "%s\\n" "$APPLIED"\n'
    )
    gcloud.chmod(0o755)
    marker = "gs://test-state/personal-data-platform/deploy/dbt-version"
    result = subprocess.run(
        [
            "bash",
            "-c",
            _workflow_step_script(
                ".github/workflows/terraform-deploy.yml", "Check dbt definitions"
            ),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "TF_STATE_BUCKET": "test-state",
            "TF_VAR_motherduck_database": "production",
            "EXPECTED_MARKER": marker,
            "APPLIED": versions[applied],
            "FORCE_DBT": str(force).lower(),
            "GITHUB_SHA": head,
            "GITHUB_ENV": str(output),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert output.read_text().splitlines() == [
        f"DBT_DEPLOY_REQUIRED={str(required).lower()}",
        f"DBT_DEPLOY_MARKER={marker}",
        f"DBT_DEPLOY_VERSION=production {dbt}",
    ]


@pytest.mark.parametrize(
    ("job_ok", "executed_image", "recorded"),
    [(True, NEXT_DIGEST, True), (False, NEXT_DIGEST, False), (True, CURRENT_DIGEST, False)],
)
def test_deploy_records_dbt_definitions_only_after_success(
    tmp_path: Path, job_ok: bool, executed_image: str, recorded: bool
) -> None:
    gcloud = tmp_path / "gcloud"
    gcloud.write_text(
        "#!/bin/bash\n"
        'if [[ "$1 $2 $3 $4" == "run jobs execute reconciliation-west" ]]; then\n'
        '  [[ "$5 $6 $7 $8" == "--project=example --region=us-west1 --args=dbt --wait" ]] || exit 90\n'
        '  [[ "$9" == "--format=value(spec.template.spec.containers[0].image)" ]] || exit 90\n'
        '  [[ "$JOB_OK" == "true" ]] || exit 1\n'
        '  printf "%s\\n" "$EXECUTED_IMAGE"; exit 0\n'
        "fi\n"
        '[[ "$*" == "storage cp - $DBT_DEPLOY_MARKER" ]] || exit 90\n'
        'cat > "$MARKER_FILE"\n'
    )
    gcloud.chmod(0o755)
    marker_file = tmp_path / "dbt-version"
    marker_file.write_text("production old\n")
    result = subprocess.run(
        [
            "bash",
            "-c",
            _workflow_step_script(
                ".github/workflows/terraform-deploy.yml", "Apply dbt definitions"
            ),
        ],
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "GCP_PROJECT_ID": "example",
            "GCP_REGION": "us-west1",
            "DBT_DEPLOY_MARKER": "gs://test-state/personal-data-platform/deploy/dbt-version",
            "DBT_DEPLOY_VERSION": "production new",
            "JOB_OK": str(job_ok).lower(),
            "TF_VAR_runtime_image_uri": NEXT_DIGEST,
            "EXECUTED_IMAGE": executed_image,
            "MARKER_FILE": str(marker_file),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is recorded, result.stderr
    assert marker_file.read_text() == ("production new\n" if recorded else "production old\n")


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
