"""
Service registry — the ground truth that maps a running Kubernetes Deployment
to the GitOps values file the agent must edit to heal it durably.

Why this matters:
  * The container name inside EVERY pod is `pharma-service` (Helm .Chart.Name),
    NOT the service name — so we cannot infer the service from the container.
  * The Deployment/Service name comes from `fullnameOverride` in each values file.
    The catalog service is the notable gotcha: its values file is
    `values-catalog-service.yaml` but the deployment is `drug-catalog-service`.

Keyed by the Kubernetes Deployment name (what we actually observe in the cluster).
"""

from dataclasses import dataclass

# The single container name shared by all 9 pods (Helm Chart.Name = pharma-service).
CONTAINER_NAME = "pharma-service"


@dataclass(frozen=True)
class Service:
    deployment: str      # Kubernetes Deployment / Service name (from fullnameOverride)
    values_file: str     # file name under envs/dev/ in the gitops repo
    ecr_repo: str        # ECR repository name (image.repository suffix)


# Deployment name -> Service metadata. This is the allow-list: the agent will
# only ever remediate deployments present here.
REGISTRY: dict[str, Service] = {
    "api-gateway":           Service("api-gateway",           "values-api-gateway.yaml",           "api-gateway"),
    "auth-service":          Service("auth-service",          "values-auth-service.yaml",          "auth-service"),
    "drug-catalog-service":  Service("drug-catalog-service",  "values-catalog-service.yaml",       "drug-catalog-service"),
    "inventory-service":     Service("inventory-service",     "values-inventory-service.yaml",     "inventory-service"),
    "manufacturing-service": Service("manufacturing-service", "values-manufacturing-service.yaml", "manufacturing-service"),
    "notification-service":  Service("notification-service",  "values-notification-service.yaml",  "notification-service"),
    "qc-service":            Service("qc-service",            "values-qc-service.yaml",            "qc-service"),
    "supplier-service":      Service("supplier-service",      "values-supplier-service.yaml",      "supplier-service"),
    "pharma-ui":             Service("pharma-ui",             "values-pharma-ui.yaml",             "pharma-ui"),
}


def lookup(deployment_name: str) -> Service | None:
    """Return the Service for a given Kubernetes deployment name, or None if unknown."""
    return REGISTRY.get(deployment_name)


def all_deployment_names() -> list[str]:
    return list(REGISTRY.keys())
