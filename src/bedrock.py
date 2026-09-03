"""
Root-cause analysis via AWS Bedrock (Amazon Nova Pro, Converse API), with a
deterministic rule-based fallback so healing still works if Bedrock is down.

The model chooses a high-level ACTION; the executor maps it to the correct
GitOps-aware remediation. Actions:
  ROLLBACK_IMAGE  -> revert image.tag in gitops (ImagePullBackOff / ErrImagePull)
  BUMP_MEMORY     -> raise resources.limits.memory in gitops (OOMKilled)
  RESTART_POD     -> delete the pod (CrashLoopBackOff / transient)
  ALERT           -> cannot auto-fix safely (e.g. CreateContainerConfigError)
"""

import json
import logging

import boto3

from . import config

log = logging.getLogger("aiops.bedrock")

VALID_ACTIONS = {"ROLLBACK_IMAGE", "BUMP_MEMORY", "RESTART_POD", "ALERT"}

# Deterministic mapping used both as the Bedrock fallback and as a guardrail
# to sanity-check the model's choice against the observed failure reason.
_REASON_TO_ACTION = {
    "ImagePullBackOff": "ROLLBACK_IMAGE",
    "ErrImagePull": "ROLLBACK_IMAGE",
    "OOMKilled": "BUMP_MEMORY",
    "CrashLoopBackOff": "RESTART_POD",
    "CreateContainerConfigError": "ALERT",
}


def rule_based(reason: str) -> dict:
    action = _REASON_TO_ACTION.get(reason, "RESTART_POD")
    return {
        "action": action,
        "root_cause": f"Rule-based mapping for {reason}",
        "fix_details": f"Deterministic remediation for {reason}",
    }


def _build_prompt(incident, deployment, current_image, previous_tag, events, logs) -> str:
    return f"""You are a Kubernetes SRE automation agent for a GitOps (ArgoCD) platform.
A pod is failing. Choose ONE remediation. The platform is deployed via ArgoCD from a git repo,
so image/resource fixes are applied by committing to git (handled by the executor), and pod
restarts are applied directly.

POD: {incident.pod_name}
DEPLOYMENT: {deployment}
NAMESPACE: {incident.namespace}
FAILURE REASON: {incident.reason}
CONTAINER STATE: {incident.container_state}
RESTART COUNT: {incident.restart_count}
CURRENT IMAGE: {current_image}
PREVIOUS KNOWN-GOOD IMAGE TAG (from git history): {previous_tag or "unknown"}

EVENTS:
{events}

LOGS (tail):
{logs[:1500]}

Available actions:
- ROLLBACK_IMAGE : bad/missing image tag (ImagePullBackOff, ErrImagePull). Executor reverts image.tag in git.
- BUMP_MEMORY    : container OOMKilled (memory limit too low). Executor raises resources.limits.memory in git.
- RESTART_POD    : transient app crash (CrashLoopBackOff not caused by image/memory). Executor deletes the pod.
- ALERT          : cannot be auto-fixed safely (e.g. missing config/secret). No change made.

Respond ONLY with JSON:
{{"action": "ROLLBACK_IMAGE|BUMP_MEMORY|RESTART_POD|ALERT", "root_cause": "one sentence", "fix_details": "what will be done"}}"""


def analyze(incident, deployment, current_image, previous_tag, events, logs) -> dict:
    """Return {action, root_cause, fix_details}. Falls back to rules on any error."""
    if not config.BEDROCK_ENABLED:
        return rule_based(incident.reason)

    prompt = _build_prompt(incident, deployment, current_image, previous_tag, events, logs)
    try:
        bedrock = boto3.client("bedrock-runtime", region_name=config.BEDROCK_REGION)
        resp = bedrock.converse(
            modelId=config.BEDROCK_MODEL,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig={"maxTokens": config.BEDROCK_MAX_TOKENS},
        )
        text = resp["output"]["message"]["content"][0]["text"]
        result = _parse_json(text)
        action = result.get("action")
        if action not in VALID_ACTIONS:
            log.warning("Bedrock returned invalid action %r; using rule-based", action)
            return rule_based(incident.reason)
        return result
    except Exception as e:
        log.error("Bedrock analysis failed (%s); using rule-based fallback", e)
        return rule_based(incident.reason)


def _parse_json(text: str) -> dict:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}") + 1
        if 0 <= start < end:
            try:
                return json.loads(text[start:end])
            except json.JSONDecodeError:
                pass
    return {}
