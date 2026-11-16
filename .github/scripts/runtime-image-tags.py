"""Keep Artifact Registry versions referenced by the PDP Cloud Run runtime."""

import argparse
import json
import os
import re
import subprocess
import sys

LIVE_TAG = re.compile(r"deployed-live-[0-9a-f]{64}")
CURRENT_TAG = "deployed-current"
ROLLBACK_TAG = "deployed-rollback"


def gcloud(*args: str) -> str:
    return subprocess.check_output(["gcloud", *args], text=True).strip()


def runtime_images(project: str, region: str) -> tuple[set[str], str | None]:
    flags = [f"--project={project}", f"--region={region}", "--format=json"]
    app = "--filter=metadata.labels.application=personal-data-platform"
    jobs = json.loads(gcloud("run", "jobs", "list", app, *flags))
    images = set()
    daily_image = None
    for job in jobs:
        containers = job["spec"]["template"]["spec"]["template"]["spec"]["containers"]
        images.update(container["image"] for container in containers)
        if job["metadata"]["name"] == "reconciliation-west":
            daily_image = containers[0]["image"]

    services = json.loads(gcloud("run", "services", "list", app, *flags))
    for service in services:
        template = service["spec"]["template"]["spec"]
        images.update(container["image"] for container in template["containers"])
        status = service.get("status", {})
        # The latest template can differ from the revision still serving traffic.
        for target in status.get("traffic", []):
            if not (target.get("percent", 0) or target.get("tag")):
                continue
            if target["revisionName"] == status.get("latestCreatedRevisionName"):
                continue  # The digest-pinned template already covers this revision.
            revision = json.loads(
                gcloud("run", "revisions", "describe", target["revisionName"], *flags)
            )
            images.update(container["image"] for container in revision["spec"]["containers"])
            images.add(revision["status"]["imageDigest"])
    return images, daily_image


def tag_image(image: str, tag: str) -> None:
    package = image.split("@", 1)[0]
    gcloud("artifacts", "docker", "tags", "add", image, f"{package}:{tag}", "--quiet")


def live_tag(image: str) -> str:
    return "deployed-live-" + image.rsplit(":", 1)[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("protect", "finish"))
    parser.add_argument("image", nargs="?")
    args = parser.parse_args()
    project = os.environ["GCP_PROJECT_ID"]
    region = os.environ["GCP_REGION"]
    package = (
        f"{region}-docker.pkg.dev/{project}/"
        f"{os.environ['ARTIFACT_REPOSITORY']}/{os.environ['IMAGE_NAME']}"
    )
    image_pattern = re.compile(re.escape(package) + r"@sha256:[0-9a-f]{64}")
    if args.image and not image_pattern.fullmatch(args.image):
        parser.error("image must be a digest in the runtime package")
    if args.action == "protect" and args.image:
        tag_image(args.image, live_tag(args.image))
        return
    if args.action == "finish" and not args.image:
        parser.error("finish requires the deployed image digest")

    references, daily_image = runtime_images(project, region)
    if args.action == "finish" and daily_image != args.image:
        raise RuntimeError("daily job does not reference the deployed image")
    versions = {
        f"{package}@{digest}"
        for digest in gcloud(
            "artifacts", "docker", "images", "list", package, "--format=value(version)"
        ).splitlines()
    }
    if args.action == "finish" and args.image not in versions:
        raise RuntimeError("deployed image is missing from the runtime registry")
    tags = {
        entry["tag"].rsplit("/", 1)[1]: f"{package}@{entry['version'].rsplit('/', 1)[1]}"
        for entry in json.loads(
            gcloud("artifacts", "docker", "tags", "list", package, "--format=json")
        )
    }
    live = {image for image in references if image_pattern.fullmatch(image)}
    for image in sorted(live):
        if image not in versions:
            print(f"Referenced image is already missing: {image}", file=sys.stderr)
        elif tags.get(live_tag(image)) != image:
            tag_image(image, live_tag(image))

    current = tags.get(CURRENT_TAG)
    if args.action == "protect":
        if not current and daily_image and daily_image in versions:
            tag_image(daily_image, CURRENT_TAG)
        return

    if current != args.image:
        if current and current in versions:
            tag_image(current, ROLLBACK_TAG)
        tag_image(args.image, CURRENT_TAG)
    for tag, image in sorted(tags.items()):
        if LIVE_TAG.fullmatch(tag) and image not in live:
            gcloud("artifacts", "docker", "tags", "delete", f"{package}:{tag}", "--quiet")


if __name__ == "__main__":
    main()
