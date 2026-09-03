"""
Kubernetes access layer: connect, detect failures, gather incident context,
resolve the owning Deployment, and perform the one safe imperative action
(delete a pod — which ArgoCD does not manage, so no drift is created).
"""

import logging
from dataclasses import dataclass

from kubernetes import client, config

log = logging.getLogger("aiops.k8s")

# Waiting-state reasons we treat as failures.
_FAIL_WAITING = {
    "CrashLoopBackOff",
    "ImagePullBackOff",
    "ErrImagePull",
    "CreateContainerConfigError",
}


@dataclass
class Incident:
    pod_name: str
    namespace: str
    reason: str            # normalized failure reason
    restart_count: int
    container_state: str
    image: str


def connect() -> tuple[client.CoreV1Api, client.AppsV1Api]:
    try:
        config.load_incluster_config()
        log.info("Loaded in-cluster Kubernetes config")
    except config.ConfigException:
        config.load_kube_config()
        log.info("Loaded local kubeconfig")
    return client.CoreV1Api(), client.AppsV1Api()


def detect_incident(pod) -> Incident | None:
    """Inspect a pod's container statuses and return an Incident if it is failing.

    Detection rules (matching the induced-failure scenarios):
      * ImagePullBackOff / ErrImagePull  -> bad image
      * OOMKilled (terminated)           -> memory too low
      * CrashLoopBackOff, or restarts>=3 & not ready -> app crash loop
      * CreateContainerConfigError       -> missing config/secret (reported, ALERT)
    """
    statuses = pod.status.container_statuses or []
    for cs in statuses:
        reason = None
        if cs.state.waiting and cs.state.waiting.reason in _FAIL_WAITING:
            reason = cs.state.waiting.reason
        elif cs.state.terminated and cs.state.terminated.reason == "OOMKilled":
            reason = "OOMKilled"
        elif (cs.restart_count or 0) >= 3 and not cs.ready:
            reason = "CrashLoopBackOff"
        # Also catch OOM recorded in lastState for a currently-restarting container.
        elif (
            cs.last_state
            and cs.last_state.terminated
            and cs.last_state.terminated.reason == "OOMKilled"
        ):
            reason = "OOMKilled"

        if reason:
            state = "Unknown"
            if cs.state.waiting:
                state = cs.state.waiting.reason or "Waiting"
            elif cs.state.running:
                state = "Running"
            elif cs.state.terminated:
                state = cs.state.terminated.reason or "Terminated"
            return Incident(
                pod_name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                reason=reason,
                restart_count=cs.restart_count or 0,
                container_state=state,
                image=(pod.spec.containers[0].image if pod.spec.containers else "unknown"),
            )
    return None


def get_pod_logs(core: client.CoreV1Api, pod_name: str, namespace: str) -> str:
    # Prefer the previous container's logs (the crashed instance).
    for previous in (True, False):
        try:
            return core.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, tail_lines=50, previous=previous
            )
        except Exception:
            continue
    return "No logs available"


def get_pod_events(core: client.CoreV1Api, pod_name: str, namespace: str) -> str:
    try:
        events = core.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
    except Exception as e:
        return f"Could not read events: {e}"
    lines = [
        f"{e.last_timestamp} | {e.reason} | {e.message}"
        for e in (events.items[-10:] if events.items else [])
    ]
    return "\n".join(lines) if lines else "No events found"


def resolve_deployment(core: client.CoreV1Api, apps: client.AppsV1Api, pod) -> str | None:
    """Walk Pod -> ReplicaSet -> Deployment to find the owning Deployment name."""
    for ref in pod.metadata.owner_references or []:
        if ref.kind == "ReplicaSet":
            try:
                rs = apps.read_namespaced_replica_set(
                    name=ref.name, namespace=pod.metadata.namespace
                )
            except Exception:
                continue
            for rs_ref in rs.metadata.owner_references or []:
                if rs_ref.kind == "Deployment":
                    return rs_ref.name
    # Fallback: the app label often equals the deployment name.
    labels = pod.metadata.labels or {}
    return labels.get("app.kubernetes.io/name") or labels.get("app")


def delete_pod(core: client.CoreV1Api, pod_name: str, namespace: str) -> None:
    """Delete a pod so its Deployment recreates it. Safe under GitOps: ArgoCD
    does not manage individual pods, so this creates no drift."""
    core.delete_namespaced_pod(name=pod_name, namespace=namespace)


def deployment_ready(apps: client.AppsV1Api, deployment: str, namespace: str) -> tuple[int, int]:
    """Return (ready_replicas, desired_replicas) for verification."""
    d = apps.read_namespaced_deployment(name=deployment, namespace=namespace)
    ready = d.status.ready_replicas or 0
    desired = d.spec.replicas or 1
    return ready, desired
