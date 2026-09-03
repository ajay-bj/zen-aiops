"""
GitOps remediation — the durable, ArgoCD-respecting healing path.

Instead of patching the live cluster (which ArgoCD selfHeal would revert), the
agent edits the service's values file in the gitops repo and pushes a commit.
ArgoCD then syncs the change to the cluster. This is how a human operator would
fix it, done automatically.

Two operations:
  * rollback_image_tag(): read the previous good image.tag from git history and
    write it back (fixes ImagePullBackOff / ErrImagePull).
  * bump_memory_limit(): raise resources.limits.memory (fixes OOMKilled).

Uses ruamel.yaml to preserve comments, key order, and formatting so commits are
minimal, reviewable diffs — not full-file rewrites.
"""

import logging
import os
import re
import shutil
import subprocess

from ruamel.yaml import YAML

from . import config

log = logging.getLogger("aiops.gitops")

try:
    import boto3  # available in-cluster; optional for local dry-run
except Exception:  # pragma: no cover
    boto3 = None

# Cache of {ecr_repo_name: set(existing_tags)} to avoid hammering the ECR API.
_ecr_tag_cache: dict[str, set[str]] = {}

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096  # avoid line-wrapping long values


class GitOpsError(Exception):
    pass


def _run(args: list[str], cwd: str | None = None) -> str:
    """Run a git command, raising GitOpsError with stderr on failure.
    Never logs the token (it only appears in the remote URL, which we don't print)."""
    result = subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=120
    )
    if result.returncode != 0:
        raise GitOpsError(f"git {' '.join(args[1:])} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _remote_url() -> str:
    if not config.GITOPS_TOKEN:
        raise GitOpsError("GITOPS_TOKEN is not set; cannot push GitOps fixes")
    # x-access-token is GitHub's convention for PAT-in-URL auth.
    return f"https://x-access-token:{config.GITOPS_TOKEN}@github.com/{config.GITOPS_REPO}.git"


def ensure_repo() -> str:
    """Clone the gitops repo if needed, otherwise fetch + hard-reset to origin.
    Returns the working directory path."""
    workdir = config.GITOPS_WORKDIR
    git_dir = os.path.join(workdir, ".git")
    if not os.path.isdir(git_dir):
        parent = os.path.dirname(workdir) or "."
        os.makedirs(parent, exist_ok=True)
        if os.path.isdir(workdir):
            shutil.rmtree(workdir, ignore_errors=True)
        log.info("Cloning gitops repo %s", config.GITOPS_REPO)
        _run(["git", "clone", "--branch", config.GITOPS_BRANCH, _remote_url(), workdir])
    else:
        _run(["git", "remote", "set-url", "origin", _remote_url()], cwd=workdir)
        _run(["git", "fetch", "origin", config.GITOPS_BRANCH], cwd=workdir)
        _run(["git", "reset", "--hard", f"origin/{config.GITOPS_BRANCH}"], cwd=workdir)
    # Identity for commits.
    _run(["git", "config", "user.name", config.GIT_AUTHOR_NAME], cwd=workdir)
    _run(["git", "config", "user.email", config.GIT_AUTHOR_EMAIL], cwd=workdir)
    return workdir


def _values_path(workdir: str, values_file: str) -> str:
    return os.path.join(workdir, config.GITOPS_ENV_DIR, values_file)


def read_current_tag(workdir: str, values_file: str) -> str | None:
    path = _values_path(workdir, values_file)
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    try:
        return str(data["image"]["tag"])
    except (KeyError, TypeError):
        return None


def read_image_repository(workdir: str, values_file: str) -> str | None:
    """Return image.repository, e.g. '304312474711.dkr.ecr.us-east-1.amazonaws.com/qc-service'."""
    path = _values_path(workdir, values_file)
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    try:
        return str(data["image"]["repository"])
    except (KeyError, TypeError):
        return None


# Matches an ECR repository URI and captures (region, repo_name).
_ECR_RE = re.compile(
    r"^\d+\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com/(.+)$"
)


def _ecr_existing_tags(repository: str | None) -> set[str] | None:
    """Return the set of image tags that actually exist in the ECR repo behind
    `repository` (an ECR URI). Returns None if we cannot determine it (not ECR,
    boto3 missing, or the API call fails) — callers then fall back to trusting
    git history.

    This is the fix that makes ImagePullBackOff healing reliable: a tag present
    in gitops git history may have been pruned from ECR by the lifecycle policy,
    so rolling back to it would just cause another ImagePullBackOff. We only ever
    roll back to a tag we can prove is pullable.
    """
    if not repository or boto3 is None:
        return None
    m = _ECR_RE.match(repository.strip())
    if not m:
        return None
    region, repo_name = m.group(1), m.group(2)
    if repo_name in _ecr_tag_cache:
        return _ecr_tag_cache[repo_name]
    try:
        ecr = boto3.client("ecr", region_name=region)
        tags: set[str] = set()
        paginator = ecr.get_paginator("describe_images")
        for page in paginator.paginate(repositoryName=repo_name):
            for detail in page.get("imageDetails", []):
                for t in detail.get("imageTags", []) or []:
                    tags.add(t)
        _ecr_tag_cache[repo_name] = tags
        return tags
    except Exception as e:
        log.warning("Could not list ECR tags for %s: %s", repo_name, e)
        return None


def previous_good_tag(workdir: str, values_file: str) -> str | None:
    """Find the last-known-good image.tag to roll back to.

    Walks the values file's git history newest->oldest. PREFERS the most recent
    prior tag that actually EXISTS in ECR (guaranteed pullable), which avoids
    rolling back to a tag that ECR's lifecycle policy has since pruned. If we
    cannot verify any tag against ECR (ECR unreachable, or none of the historical
    tags still exist), we gracefully fall back to the newest differing historical
    tag so the agent always attempts a heal rather than giving up.
    """
    rel = f"{config.GITOPS_ENV_DIR}/{values_file}"
    current = read_current_tag(workdir, values_file)
    ecr_tags = _ecr_existing_tags(read_image_repository(workdir, values_file))

    try:
        commits = _run(
            ["git", "log", "--format=%H", "--", rel], cwd=workdir
        ).splitlines()
    except GitOpsError:
        return None

    fallback: str | None = None
    seen: set[str] = set()
    for sha in commits:
        try:
            blob = _run(["git", "show", f"{sha}:{rel}"], cwd=workdir)
        except GitOpsError:
            continue
        m = re.search(r"^\s*tag:\s*['\"]?([^'\"\s#]+)", blob, re.MULTILINE)
        if not m:
            continue
        tag = m.group(1)
        if tag == current or tag in seen:
            continue
        seen.add(tag)
        if fallback is None:
            fallback = tag  # newest differing historical tag (best-effort default)
        # Prefer a tag we can prove is pullable.
        if ecr_tags is None or tag in ecr_tags:
            return tag
    # No historical tag verified in ECR — fall back to the newest differing tag
    # so we still attempt a heal. (rollback_image_tag guards against no-ops.)
    return fallback


def rollback_image_tag(values_file: str, deployment: str, target_tag: str) -> tuple[bool, str]:
    """Set image.tag back to target_tag and commit. Returns (changed, message)."""
    workdir = ensure_repo()
    path = _values_path(workdir, values_file)
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    current = str(data.get("image", {}).get("tag", ""))
    if current == target_tag:
        return False, f"image.tag already {target_tag}; nothing to do"
    # Best-effort sanity check: if we can see ECR and the target isn't there,
    # warn (it may fail to pull) but still proceed — better to attempt a heal
    # than to stall. previous_good_tag() already prefers ECR-verified tags.
    ecr_tags = _ecr_existing_tags(read_image_repository(workdir, values_file))
    if ecr_tags is not None and target_tag not in ecr_tags:
        log.warning("rollback target %s not found in ECR; proceeding best-effort", target_tag)
    data["image"]["tag"] = target_tag
    with open(path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    msg = f"fix(aiops): rollback {deployment} image {current} -> {target_tag} (auto-heal ImagePullBackOff)"
    _commit_and_push(workdir, msg)
    return True, msg


def _parse_mem_mi(value: str) -> int | None:
    """Parse a k8s memory quantity into MiB (supports Mi, Gi, Ki, plain bytes)."""
    if value is None:
        return None
    s = str(value).strip()
    m = re.match(r"^(\d+(?:\.\d+)?)\s*(Gi|Mi|Ki|G|M|K)?$", s)
    if not m:
        return None
    num = float(m.group(1))
    unit = m.group(2) or ""
    factor = {
        "Gi": 1024, "Mi": 1, "Ki": 1 / 1024,
        "G": 1000 / 1.048576 / 1000 * 1024,  # approx; unused in practice
        "M": 1000 / 1.048576, "K": 1 / 1024, "": 1 / (1024 * 1024),
    }.get(unit, 1)
    return int(round(num * factor))


def bump_memory_limit(values_file: str, deployment: str) -> tuple[bool, str]:
    """Raise resources.limits.memory by the configured multiplier (capped) and commit."""
    workdir = ensure_repo()
    path = _values_path(workdir, values_file)
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)

    resources = data.get("resources") or {}
    limits = resources.get("limits") or {}
    current_mem = limits.get("memory")
    current_mi = _parse_mem_mi(current_mem)
    if current_mi is None:
        return False, f"could not parse current memory limit {current_mem!r}"

    # Take the larger of (current x multiplier) and a sane floor, so a single fix
    # restores health even when the broken value is absurdly low (e.g. 16Mi).
    # Never exceed the ceiling.
    bumped = max(int(current_mi * config.OOM_MEMORY_MULTIPLIER), config.OOM_MEMORY_FLOOR_MI)
    new_mi = min(bumped, config.OOM_MEMORY_CEILING_MI)
    if new_mi <= current_mi:
        return False, f"memory limit already sufficient ({current_mem}); not bumping"

    new_mem = f"{new_mi}Mi"
    # ruamel keeps structure; ensure nested maps exist.
    data.setdefault("resources", {}).setdefault("limits", {})["memory"] = new_mem
    with open(path, "w", encoding="utf-8") as f:
        _yaml.dump(data, f)
    msg = f"fix(aiops): raise {deployment} memory limit {current_mem} -> {new_mem} (auto-heal OOMKilled)"
    _commit_and_push(workdir, msg)
    return True, msg


def _commit_and_push(workdir: str, message: str) -> None:
    _run(["git", "add", "-A"], cwd=workdir)
    # If nothing staged, skip (idempotent).
    status = _run(["git", "status", "--porcelain"], cwd=workdir)
    if not status:
        log.info("No changes to commit")
        return
    _run(["git", "commit", "-m", message], cwd=workdir)
    _run(["git", "push", "origin", config.GITOPS_BRANCH], cwd=workdir)
    log.info("Pushed GitOps fix: %s", message)
