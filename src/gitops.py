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


def _known_bad_tags(workdir: str) -> set[str]:
    """Tags the agent has already rolled AWAY from are known-bad and must never
    be chosen again as a rollback target. We recover them from our own commit
    messages, which have the form:

        fix(aiops): rollback <deployment> image <BAD> -> <GOOD> (auto-heal ...)

    The left-hand tag (<BAD>) is the one that was failing. Without this, a stale
    replica still pinned to a bad tag can make the agent re-select that bad tag
    as "previous good", causing an endless good<->bad oscillation.
    """
    bad: set[str] = set()
    try:
        subjects = _run(
            ["git", "log", "--format=%s", "-n", "200"], cwd=workdir
        ).splitlines()
    except GitOpsError:
        return bad
    pat = re.compile(r"rollback\s+\S+\s+image\s+(\S+)\s*->")
    for subj in subjects:
        m = pat.search(subj)
        if m:
            bad.add(m.group(1))
    return bad


def previous_good_tag(workdir: str, values_file: str) -> str | None:
    """Find the most recent PRIOR image.tag for this values file from git history
    that is neither the current tag nor a known-bad tag — i.e. the last-known-good
    tag we can safely roll back to."""
    rel = f"{config.GITOPS_ENV_DIR}/{values_file}"
    current = read_current_tag(workdir, values_file)
    bad = _known_bad_tags(workdir)
    bad.discard("")  # defensive
    if current:
        bad.add(current)  # never roll back to the tag that's failing right now
    # Walk commit history for this file and return the first tag that is safe.
    try:
        commits = _run(
            ["git", "log", "--format=%H", "--", rel], cwd=workdir
        ).splitlines()
    except GitOpsError:
        return None
    for sha in commits:
        try:
            blob = _run(["git", "show", f"{sha}:{rel}"], cwd=workdir)
        except GitOpsError:
            continue
        m = re.search(r"^\s*tag:\s*['\"]?([^'\"\s#]+)", blob, re.MULTILINE)
        if m and m.group(1) not in bad:
            return m.group(1)
    return None


def rollback_image_tag(values_file: str, deployment: str, target_tag: str) -> tuple[bool, str]:
    """Set image.tag back to target_tag and commit. Returns (changed, message)."""
    workdir = ensure_repo()
    # Never roll back TO a tag we've already proven bad — prevents oscillation.
    if target_tag in _known_bad_tags(workdir):
        return False, f"refusing rollback to known-bad tag {target_tag}"
    path = _values_path(workdir, values_file)
    with open(path, "r", encoding="utf-8") as f:
        data = _yaml.load(f)
    current = str(data.get("image", {}).get("tag", ""))
    if current == target_tag:
        return False, f"image.tag already {target_tag}; nothing to do"
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
