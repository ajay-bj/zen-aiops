"""
Central configuration for the AIOps agent.

Everything is environment-driven so the same image runs unchanged in the cluster
and locally (for dry-run testing). Nothing sensitive is hardcoded — the GitOps
token comes from a mounted Kubernetes Secret, AWS creds come from IRSA.
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ── What to watch ────────────────────────────────────────────────────────────
# The pharma platform runs in the `dev` namespace on pharma-dev-cluster.
NAMESPACE = os.getenv("WATCH_NAMESPACE", "dev")
CHECK_INTERVAL = _int("CHECK_INTERVAL_SECONDS", 30)

# ── Bedrock (root-cause analysis) ────────────────────────────────────────────
BEDROCK_REGION = os.getenv("BEDROCK_REGION", "us-east-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "amazon.nova-pro-v1:0")
BEDROCK_MAX_TOKENS = _int("BEDROCK_MAX_TOKENS", 500)
# If Bedrock is unreachable, fall back to deterministic rule-based remediation.
BEDROCK_ENABLED = _bool("BEDROCK_ENABLED", True)

# ── GitOps remediation (the durable, ArgoCD-respecting path) ─────────────────
# The agent commits fixes to the gitops repo; ArgoCD then syncs them.
GITOPS_REPO = os.getenv("GITOPS_REPO", "ajay-bj/zen-gitops-ajay")
GITOPS_BRANCH = os.getenv("GITOPS_BRANCH", "main")
GITOPS_ENV_DIR = os.getenv("GITOPS_ENV_DIR", "envs/dev")
# GitHub PAT with contents:write on the gitops repo (mounted from a K8s Secret).
GITOPS_TOKEN = os.getenv("GITOPS_TOKEN", "")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "aiops-agent[bot]")
GIT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "aiops-agent@users.noreply.github.com")
# Local working checkout inside the container.
GITOPS_WORKDIR = os.getenv("GITOPS_WORKDIR", "/app/_gitops_work")
# How much to bump memory limits on OOMKilled (multiplier), a sane floor so one
# fix restores health even from an absurdly-low broken value, and a hard ceiling.
OOM_MEMORY_MULTIPLIER = float(os.getenv("OOM_MEMORY_MULTIPLIER", "2.0"))
OOM_MEMORY_FLOOR_MI = _int("OOM_MEMORY_FLOOR_MI", 512)
OOM_MEMORY_CEILING_MI = _int("OOM_MEMORY_CEILING_MI", 2048)

# ── Safety guardrails ────────────────────────────────────────────────────────
# DRY_RUN: analyze + log the intended fix but do NOT change anything.
DRY_RUN = _bool("DRY_RUN", False)
# Per-pod cooldown so one flapping pod can't trigger a storm of fixes.
COOLDOWN_SECONDS = _int("COOLDOWN_SECONDS", 300)
# Seconds to wait before verifying recovery.
VERIFY_DELAY_SECONDS = _int("VERIFY_DELAY_SECONDS", 45)
# Only ever act on this exact set of services (defense in depth). Comma-separated
# override via ALLOWED_SERVICES; empty means "use the built-in known set".
_allowed_env = os.getenv("ALLOWED_SERVICES", "").strip()
ALLOWED_SERVICES_OVERRIDE = (
    [s.strip() for s in _allowed_env.split(",") if s.strip()] if _allowed_env else None
)

# ── Health endpoint ──────────────────────────────────────────────────────────
HEALTH_PORT = _int("HEALTH_PORT", 8000)
