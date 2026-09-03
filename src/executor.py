"""
Executor — turns a chosen action into the correct GitOps-aware remediation.

Routing:
  ROLLBACK_IMAGE -> gitops.rollback_image_tag (commit; ArgoCD syncs)
  BUMP_MEMORY    -> gitops.bump_memory_limit  (commit; ArgoCD syncs)
  RESTART_POD    -> k8s delete pod            (Deployment recreates; no drift)
  ALERT          -> log only (cannot auto-fix safely)

Guardrails:
  * DRY_RUN short-circuits every mutating action.
  * Only services in the registry allow-list are touched.
  * Bedrock's action is cross-checked against the observed failure reason so a
    model mistake can't, e.g., delete a pod for an ImagePullBackOff.
"""

import logging

from . import config, gitops, k8s_client
from .bedrock import _REASON_TO_ACTION
from .services import Service

log = logging.getLogger("aiops.executor")


def _coerce_action(chosen: str, reason: str) -> str:
    """Prefer the deterministic action for the observed reason; only trust the
    model when it agrees or when the reason has no strong mapping."""
    deterministic = _REASON_TO_ACTION.get(reason)
    if deterministic and chosen != deterministic:
        log.warning(
            "Overriding model action %r with reason-based %r for %s",
            chosen, deterministic, reason,
        )
        return deterministic
    return chosen


def execute(core, apps, incident, svc: Service, analysis: dict, previous_tag: str | None) -> str:
    action = _coerce_action(analysis.get("action", "RESTART_POD"), incident.reason)

    if config.DRY_RUN:
        log.info("[DRY_RUN] Would execute %s for %s (%s)", action, svc.deployment, incident.reason)
        return f"DRY_RUN:{action}"

    if action == "ROLLBACK_IMAGE":
        return _rollback(svc, previous_tag)
    if action == "BUMP_MEMORY":
        return _bump_memory(svc)
    if action == "RESTART_POD":
        return _restart(core, incident)
    if action == "ALERT":
        log.warning("ALERT: %s needs manual attention (%s) — no safe auto-fix",
                    svc.deployment, incident.reason)
        return "ALERTED"

    log.warning("Unknown action %r; defaulting to RESTART_POD", action)
    return _restart(core, incident)


def _rollback(svc: Service, previous_tag: str | None) -> str:
    if not previous_tag:
        return "SKIPPED: no previous known-good image tag found in git history"
    changed, msg = gitops.rollback_image_tag(svc.values_file, svc.deployment, previous_tag)
    log.info("  %s", msg)
    return "ROLLED_BACK" if changed else f"NOOP: {msg}"


def _bump_memory(svc: Service) -> str:
    changed, msg = gitops.bump_memory_limit(svc.values_file, svc.deployment)
    log.info("  %s", msg)
    return "MEMORY_RAISED" if changed else f"NOOP: {msg}"


def _restart(core, incident) -> str:
    log.info("  Deleting pod %s (Deployment will recreate it)", incident.pod_name)
    k8s_client.delete_pod(core, incident.pod_name, incident.namespace)
    return "RESTARTED"
