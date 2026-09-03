# zen-aiops — Agent Testing (Self-Heal Demos)

Two end-to-end self-healing demos, each shown as: **(1) how to induce the failure**, then
**(2) the actual agent log output** proving it detected and healed the incident.

> The sample log blocks below are **real captured output** from a reference run, so they contain that
> account's ID, image tags, and pod names (e.g. `304312474711`, `sha-3ceb27b`, `qc-service-...-xxxxx`).
> Yours will differ — that's expected. What matters is the pattern: `🚨 INCIDENT → 🧠 action → fix →
> ✅ HEALED`.

## Before you start — your values + which shell

These demos use the same placeholders as `IMPLEMENTATION.md`. Substitute your real values (or
`export` / set them once, as shown there):

| Placeholder | What it is | Example |
|---|---|---|
| `<NAMESPACE>` | namespace the pharma apps run in | `dev` |
| `<GITOPS_REPO>` | your fork of the gitops repo | `your-user/zen-gitops` |
| `<TEST_SVC>` | a service with ArgoCD `selfHeal` **OFF** (safe to break) | `qc-service` |
| `<REGION>` | AWS region of your ECR | `us-east-1` |

**Set them once (optional but handy):**

Linux / macOS / Git Bash / WSL:
```bash
export NAMESPACE="dev"
export GITOPS_REPO="your-user/zen-gitops"
export TEST_SVC="qc-service"
```
Windows PowerShell:
```powershell
$NAMESPACE  = "dev"
$GITOPS_REPO= "your-user/zen-gitops"
$TEST_SVC   = "qc-service"
```

**Why `<TEST_SVC>` = `qc-service`:** it has ArgoCD `selfHeal` **OFF**, so a failure we induce stays
put and the agent is unambiguously the thing that heals it. On a `selfHeal`-ON service, ArgoCD would
revert a live break before the agent could act. We break it the GitOps way (a real commit) or by
killing the process — exactly how failures happen in reality — and the agent heals it the GitOps way
(a commit ArgoCD syncs) or by restarting the pod.

> Requires `yq` installed for the image demos (`git`, `kubectl`, `gh` you already have).

## Setup — keep the agent log open + clone your gitops repo

In one terminal, stream the agent log so you can watch the play-by-play:

Linux / macOS / Git Bash / WSL:
```bash
kubectl logs -f deployment/aiops-agent -n $NAMESPACE
```
Windows PowerShell:
```powershell
kubectl logs -f deployment/aiops-agent -n $NAMESPACE
```

In a second terminal, get a local checkout of **your gitops repo** — you'll edit + commit here:

Linux / macOS / Git Bash / WSL:
```bash
gh repo clone $GITOPS_REPO _zg
cd _zg
```
Windows PowerShell:
```powershell
gh repo clone $GITOPS_REPO _zg
cd _zg
```

> The container inside every pharma pod is named **`pharma-service`** (the Helm chart name),
> not the service name — that's why `kubectl exec -c` uses `pharma-service`.

---

## Demo 1 — CrashLoopBackOff (process crash) → RESTART_POD

The best on-screen demo: the agent deletes the crashing pod directly and the Deployment recreates it
in seconds — no ArgoCD wait, so the heal is near-instant.

### Induce — kill PID 1 in the running container so it crash-loops

Linux / macOS / Git Bash / WSL:
```bash
kubectl exec deployment/$TEST_SVC -n $NAMESPACE -c pharma-service -- kill 1
# run 2–3 times if the first restart recovers, to push it into CrashLoopBackOff
```
Windows PowerShell:
```powershell
kubectl exec deployment/$TEST_SVC -n $NAMESPACE -c pharma-service -- kill 1
# run 2–3 times if the first restart recovers, to push it into CrashLoopBackOff
```

### Agent output — detects, analyzes, restarts, verifies

```text
🚨 INCIDENT  pod=qc-service-7cdbccf57f-cftqx  reason=CrashLoopBackOff  ns=dev
  deployment=qc-service  values=values-qc-service.yaml  prev_good_tag=sha-4088c8f
  🧠 action=ROLLBACK_IMAGE | root_cause=The current image is causing the pod to fail due to a readiness probe failure.
Overriding model action 'ROLLBACK_IMAGE' with reason-based 'RESTART_POD' for CrashLoopBackOff
  Deleting pod qc-service-7cdbccf57f-cftqx (Deployment will recreate it)
  → result=RESTARTED
  ⏳ verifying in 20s...
  ✅ HEALED: qc-service 1/1 ready
```

**What to point out:**
- The agent detected `CrashLoopBackOff` on its own.
- Bedrock (Nova Pro) suggested `ROLLBACK_IMAGE`, but the **guardrail overrode it** to
  `RESTART_POD` because the observed reason is a crash loop, not a bad image. This is the
  reason-based safety cross-check — a model mistake can't cause the wrong action.
- The pod was deleted and recreated by the Deployment; it came back `1/1 Running`.

### Verify healed

Linux / macOS / Git Bash / WSL:
```bash
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/name=$TEST_SVC   # fresh pod, 1/1 Running
```
Windows PowerShell:
```powershell
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/name=$TEST_SVC   # fresh pod, 1/1 Running
```

---

## Demo 2 — ImagePullBackOff (bad image tag) → ROLLBACK_IMAGE

The agent fixes this the GitOps way: it commits a rollback to a **previous image tag that actually
exists in ECR**, and ArgoCD syncs it. Allow ~1–2 min (ArgoCD's sync cadence adds ~30–90s — normal
and outside the agent).

### Induce — point the service at a tag that doesn't exist (via gitops → ArgoCD applies it)

Linux / macOS / Git Bash / WSL:
```bash
yq -i '.image.tag = "sha-broken999"' envs/$NAMESPACE/values-$TEST_SVC.yaml
git commit -am "test: break $TEST_SVC image (induce ImagePullBackOff)"
git push
```
Windows PowerShell:
```powershell
yq -i ".image.tag = `"sha-broken999`"" envs/$NAMESPACE/values-$TEST_SVC.yaml
git commit -am "test: break $TEST_SVC image (induce ImagePullBackOff)"
git push
```
> No `yq`? Edit `envs/<NAMESPACE>/values-<TEST_SVC>.yaml` by hand: set `image.tag: sha-broken999`,
> then `git commit -am "..."` and `git push`.

### Agent output — detects, rolls back to an ECR-verified tag, verifies, then holds

```text
🚨 INCIDENT  pod=qc-service-85f645696f-sw7ts  reason=ErrImagePull  ns=dev
  deployment=qc-service  values=values-qc-service.yaml  prev_good_tag=sha-3ceb27b
  🧠 action=ROLLBACK_IMAGE | root_cause=The current image 304312474711.dkr.ecr.us-east-1.amazonaws.com/qc-service:sha-broken999 is not found, causing ErrImagePull.
  Pushed GitOps fix: fix(aiops): rollback qc-service image sha-broken999 -> sha-3ceb27b (auto-heal ImagePullBackOff)
  → result=ROLLED_BACK
  ⏳ verifying in 20s...
  ✅ HEALED: qc-service 1/1 ready

🚨 INCIDENT  pod=qc-service-85f645696f-sw7ts  reason=ImagePullBackOff  ns=dev
  qc-service image rolled back recently; waiting for ArgoCD to converge (cooldown)
```

**What to point out:**
- The agent detected the bad pull and Bedrock chose `ROLLBACK_IMAGE`.
- `prev_good_tag` is a tag that **actually exists in ECR** — the agent queries ECR and skips any
  historical tag the ECR lifecycle policy has since pruned, so the rollback always lands on a
  pullable image.
- It made **exactly one** rollback commit, then entered the per-deployment **rollback cooldown**.
  The `waiting for ArgoCD to converge (cooldown)` line is expected and correct — it prevents the
  agent from thrashing while ArgoCD finishes syncing and cleaning up the old ReplicaSet. No
  oscillation.

### Verify healed

Linux / macOS / Git Bash / WSL:
```bash
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/name=$TEST_SVC       # 1/1 Running on a good tag
git pull --ff-only && grep 'tag:' envs/$NAMESPACE/values-$TEST_SVC.yaml  # back to a good sha
```
Windows PowerShell:
```powershell
kubectl get pods -n $NAMESPACE -l app.kubernetes.io/name=$TEST_SVC
git pull --ff-only; Select-String -Path "envs/$NAMESPACE/values-$TEST_SVC.yaml" -Pattern 'tag:'
```

---

## Reset after testing (optional)

The agent already committed working values, so nothing needs undoing. To force a specific
known-good state back, set `image.tag` to a tag that exists in your ECR (replace `sha-XXXXXXX`):

Linux / macOS / Git Bash / WSL:
```bash
git pull --ff-only
yq -i '.image.tag = "sha-XXXXXXX"' envs/$NAMESPACE/values-$TEST_SVC.yaml
git commit -am "reset $TEST_SVC to known-good" && git push
```
Windows PowerShell:
```powershell
git pull --ff-only
yq -i ".image.tag = `"sha-XXXXXXX`"" envs/$NAMESPACE/values-$TEST_SVC.yaml
git commit -am "reset $TEST_SVC to known-good"; git push
```
> Find a tag that exists in ECR with:
> `aws ecr describe-images --repository-name <TEST_SVC> --region <REGION> --query "imageDetails[].imageTags" --output text`

---

## How the healing actions map

| Failure | Detected reason | Agent action | How it's applied |
|---|---|---|---|
| Process crash | `CrashLoopBackOff` | `RESTART_POD` | delete the pod (Deployment recreates it) — no ArgoCD wait |
| Bad/missing image tag | `ImagePullBackOff` / `ErrImagePull` | `ROLLBACK_IMAGE` | commit a rollback to an ECR-verified prior tag → ArgoCD syncs |

> The agent uses AWS Bedrock (Amazon Nova Pro) for root-cause analysis, with a deterministic
> rule-based fallback if Bedrock is unavailable — and a reason-based guardrail that overrides the
> model when its choice doesn't match the observed failure. So healing is reliable even if the
> model is down or wrong.
