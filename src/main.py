"""
Zen Pharma AIOps Self-Healing Agent — GitOps-aware entrypoint.

Loop: watch dev namespace -> detect failure -> gather context -> Bedrock RCA
      -> execute GitOps-aware remediation -> verify recovery.

Design principle: cooperate with ArgoCD.
  * Image / memory fixes are committed to the gitops repo (ArgoCD applies them),
    because live patches would be reverted by ArgoCD selfHeal.
  * Pod restarts are done directly (ArgoCD does not own individual pods).
"""

import json
import logging
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread

from . import bedrock, config, executor, gitops, k8s_client
from . import services as svc_registry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-5s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("aiops")

_recent: dict[str, float] = {}


def _on_cooldown(pod_name: str) -> bool:
    return (time.time() - _recent.get(pod_name, 0)) < config.COOLDOWN_SECONDS


def _mark(pod_name: str) -> None:
    _recent[pod_name] = time.time()


def _allowed(deployment: str) -> bool:
    if config.ALLOWED_SERVICES_OVERRIDE is not None:
        return deployment in config.ALLOWED_SERVICES_OVERRIDE
    return svc_registry.lookup(deployment) is not None


def handle_incident(core, apps, pod, incident) -> None:
    log.info("─" * 64)
    log.info("🚨 INCIDENT  pod=%s  reason=%s  ns=%s",
             incident.pod_name, incident.reason, incident.namespace)

    deployment = k8s_client.resolve_deployment(core, apps, pod)
    if not deployment:
        log.warning("Could not resolve owning Deployment for %s; skipping", incident.pod_name)
        return

    svc = svc_registry.lookup(deployment)
    if not _allowed(deployment) or svc is None:
        log.warning("Deployment %s not in the managed allow-list; skipping (safety)", deployment)
        return

    _mark(incident.pod_name)

    # ── Gather context ──
    events = k8s_client.get_pod_events(core, incident.pod_name, incident.namespace)
    logs = k8s_client.get_pod_logs(core, incident.pod_name, incident.namespace)

    # Previous known-good image tag (from gitops git history) — needed for rollback.
    previous_tag = None
    try:
        workdir = gitops.ensure_repo()
        previous_tag = gitops.previous_good_tag(workdir, svc.values_file)
    except Exception as e:
        log.warning("Could not read gitops history for %s: %s", svc.values_file, e)

    log.info("  deployment=%s  values=%s  prev_good_tag=%s",
             deployment, svc.values_file, previous_tag)

    # ── Analyze (Bedrock, with rule-based fallback) ──
    analysis = bedrock.analyze(
        incident, deployment, incident.image, previous_tag, events, logs
    )
    log.info("  🧠 action=%s | root_cause=%s",
             analysis.get("action"), analysis.get("root_cause"))

    # ── Execute ──
    result = executor.execute(core, apps, incident, svc, analysis, previous_tag)
    log.info("  → result=%s", result)

    # ── Verify ──
    if result in ("RESTARTED", "ROLLED_BACK", "MEMORY_RAISED"):
        log.info("  ⏳ verifying in %ss...", config.VERIFY_DELAY_SECONDS)
        time.sleep(config.VERIFY_DELAY_SECONDS)
        try:
            ready, desired = k8s_client.deployment_ready(apps, deployment, incident.namespace)
            if ready >= desired:
                log.info("  ✅ HEALED: %s %d/%d ready", deployment, ready, desired)
            else:
                log.info("  ⚠️  %s %d/%d ready — ArgoCD sync may still be in progress",
                         deployment, ready, desired)
        except Exception as e:
            log.warning("  verify error: %s", e)
    log.info("─" * 64)


def watch_loop(core, apps) -> None:
    log.info("=" * 64)
    log.info("  ZEN PHARMA AIOps SELF-HEALING AGENT (GitOps-aware)")
    log.info("  namespace=%s  interval=%ss  dry_run=%s",
             config.NAMESPACE, config.CHECK_INTERVAL, config.DRY_RUN)
    log.info("  bedrock=%s model=%s  gitops_repo=%s",
             config.BEDROCK_ENABLED, config.BEDROCK_MODEL, config.GITOPS_REPO)
    log.info("  managed services: %s", ", ".join(svc_registry.all_deployment_names()))
    log.info("=" * 64)

    while True:
        try:
            pods = core.list_namespaced_pod(namespace=config.NAMESPACE)
            for pod in pods.items:
                if "aiops" in (pod.metadata.name or ""):
                    continue  # never act on ourselves
                incident = k8s_client.detect_incident(pod)
                if incident and not _on_cooldown(incident.pod_name):
                    handle_incident(core, apps, pod, incident)
        except Exception as e:
            log.error("watch loop error: %s", e)
        time.sleep(config.CHECK_INTERVAL)


class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({
            "status": "healthy",
            "agent": "zen-aiops",
            "namespace": config.NAMESPACE,
            "dry_run": config.DRY_RUN,
            "model": config.BEDROCK_MODEL,
        }).encode())

    def log_message(self, *_):
        pass


def _serve_health():
    HTTPServer(("0.0.0.0", config.HEALTH_PORT), _Health).serve_forever()


def main():
    Thread(target=_serve_health, daemon=True).start()
    log.info("health endpoint on :%s", config.HEALTH_PORT)
    core, apps = k8s_client.connect()
    watch_loop(core, apps)


if __name__ == "__main__":
    main()
