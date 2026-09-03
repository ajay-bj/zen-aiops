# zen-aiops — Step-by-Step Implementation Guide (DEV only)
# zen-aiops — Step-by-Step Implementation Guide (DEV only)

Follow these steps **in order, top to bottom**. Each block is copy-paste runnable.
Only two things are yours to supply: your **AWS keys** (GitHub repo secrets, Step 3) and a
**GitHub PAT** (Step 6). Everything else is fixed for this account (`304312474711`, `us-east-1`,
cluster `pharma-dev-cluster`, namespace `dev`).

## Where to run these commands (READ FIRST)

**Run every command from inside the `zen-aiops` repo folder** — several steps use relative paths
like `iam/trust-policy.template.json`, `argocd/...`, and `k8s/manifests/...`, which only resolve
from the repo root.

```bash
# clone your repo (or cd into your existing local copy), then cd into it:
git clone https://github.com/ajay-bj/zen-aiops.git   # skip if you already have it
cd zen-aiops
```

**Shell:**
- **Linux / macOS:** use your normal terminal (bash/zsh). All commands work as written.
- **Windows:** use **Git Bash** or **WSL** (NOT PowerShell) — the `$(...)`, `sed`, and single-quoted
  JSON in Steps 5 & 6 are bash syntax. Every step below includes a **Windows PowerShell alternative**
  where the syntax differs, if you must use PowerShell.

Confirm you're in the right place:
```bash
ls        # you should see: src  k8s  argocd  iam  Dockerfile  README.md  IMPLEMENTATION.md
```

Prereqs: `aws`, `kubectl`, `git`, `gh`, Docker installed; you can reach AWS + the cluster.

---

## Prerequisites (already true for this platform — just confirm)
- The pharma platform is deployed: 9 apps Running in `dev`, ArgoCD + External Secrets Operator installed.
- Tools installed: `aws`, `kubectl`, `git`, `gh` (GitHub CLI), Docker.
- You can reach the cluster and AWS:
```bash
aws sts get-caller-identity
aws eks update-kubeconfig --region us-east-1 --name pharma-dev-cluster
kubectl get pods -n dev
```

---

## Step 1 — Publish this code to GitHub (skip if the repo already exists)

**If `ajay-bj/zen-aiops` does NOT exist yet** (first-time author) — run from inside the local
`zen-aiops/` folder that contains this code:
```bash
git init -b main
git add .
git commit -m "feat: zen-aiops self-healing agent"
gh repo create ajay-bj/zen-aiops --public --source=. --remote=origin --push
```

**If the repo ALREADY exists** and you cloned it (per "Where to run" above), skip this step — you're
already in the folder and it's on GitHub.

## Step 2 — Enable GitHub Actions on the new repo
```bash
gh api -X PUT repos/ajay-bj/zen-aiops/actions/permissions -f enabled=true -f allowed_actions=all
```

## Step 3 — Add AWS keys so CI can push the image to ECR
**Reuse the SAME AWS keys you already use for `zen-infra`** (the terraform/iamadmin keys) — no new
IAM user needed. Paste those existing values:
```bash
gh secret set AWS_ACCESS_KEY_ID     --repo ajay-bj/zen-aiops --body "YOUR_EXISTING_AWS_ACCESS_KEY_ID"
gh secret set AWS_SECRET_ACCESS_KEY --repo ajay-bj/zen-aiops --body "YOUR_EXISTING_AWS_SECRET_ACCESS_KEY"
```

## Step 4 — Build & push the agent image (trigger CI, then wait for it)
```bash
gh workflow run build.yml --repo ajay-bj/zen-aiops
# wait ~2 min, then confirm the image landed:
aws ecr describe-images --repository-name aiops-agent --region us-east-1 \
  --query "imageDetails[].imageTags" --output text
```
You should see `latest` (and a `sha-...` tag). If the ECR repo didn't exist, CI created it.

## Step 5 — Create the Bedrock IAM role (IRSA) — one block, copy-paste
(Run from the `zen-aiops` folder — it reads `iam/*.json`.)

**Linux / macOS / Git Bash / WSL:**
```bash
OIDC=$(aws eks describe-cluster --name pharma-dev-cluster --region us-east-1 \
  --query "cluster.identity.oidc.issuer" --output text | awk -F'/id/' '{print $2}') && \
sed "s/<OIDC_ID>/$OIDC/g" iam/trust-policy.template.json > /tmp/aiops-trust.json && \
aws iam create-role --role-name pharma-dev-aiops-role \
  --assume-role-policy-document file:///tmp/aiops-trust.json && \
aws iam put-role-policy --role-name pharma-dev-aiops-role \
  --policy-name aiops-bedrock --policy-document file://iam/bedrock-policy.json && \
echo "IAM role pharma-dev-aiops-role ready"
```

**Windows PowerShell (equivalent):**
```powershell
$OIDC = (aws eks describe-cluster --name pharma-dev-cluster --region us-east-1 `
  --query "cluster.identity.oidc.issuer" --output text) -split '/id/' | Select-Object -Last 1
(Get-Content iam/trust-policy.template.json) -replace '<OIDC_ID>', $OIDC | Set-Content "$env:TEMP\aiops-trust.json"
aws iam create-role --role-name pharma-dev-aiops-role `
  --assume-role-policy-document "file://$env:TEMP\aiops-trust.json"
aws iam put-role-policy --role-name pharma-dev-aiops-role `
  --policy-name aiops-bedrock --policy-document file://iam/bedrock-policy.json
Write-Host "IAM role pharma-dev-aiops-role ready"
```

## Step 6 — Put the GitOps PAT in AWS Secrets Manager
**Reuse the SAME PAT you already created as `GITOPS_TOKEN`** for the backend/frontend CI (it already
has `contents:write` on `ajay-bj/zen-gitops-ajay` — exactly what the agent needs). No new token.
Paste that existing token value.

**Linux / macOS / Git Bash / WSL:**
```bash
aws secretsmanager create-secret \
  --name /pharma/dev/aiops-gitops-token \
  --secret-string '{"token":"YOUR_EXISTING_GITOPS_PAT"}' \
  --region us-east-1
```

**Windows PowerShell (equivalent — note the escaped quotes):**
```powershell
aws secretsmanager create-secret `
  --name /pharma/dev/aiops-gitops-token `
  --secret-string '{\"token\":\"YOUR_EXISTING_GITOPS_PAT\"}' `
  --region us-east-1
```
> This stores the token in AWS Secrets Manager so ESO can inject it into the agent pod at runtime.
> (The CI copy lives in GitHub secrets; the pod copy must live in Secrets Manager — same token value,
> two consumers.)

## Step 7 — Deploy via ArgoCD (one-time apply; everything else auto-syncs)
```bash
kubectl apply -f argocd/aiops-project.yaml
kubectl apply -f argocd/aiops-agent-app.yaml
```

## Step 8 — Verify it's healthy
```bash
kubectl get application aiops-agent-dev -n argocd          # SYNCED / HEALTHY
kubectl get externalsecret aiops-gitops-token -n dev       # READY=True
kubectl get pods -n dev -l app=aiops-agent                 # 1/1 Running
kubectl logs -f deployment/aiops-agent -n dev              # startup banner
```

**Done.** The agent is now watching all 9 pods in `dev` and will auto-heal them.

---

## Demo tips (fast + visual)
- The agent ships with **demo timings**: scans every **15s**, verifies after **20s**, cooldown **60s**
  (in `k8s/manifests/deployment.yaml`). For production raise these to 30 / 45 / 300.
- **Best first demo = CrashLoopBackOff (Test 2):** the agent deletes the pod directly and the
  Deployment recreates it in seconds — no ArgoCD wait, so the heal is near-instant on screen.
- Image/OOM demos go through git → ArgoCD, so allow ~1–2 min total (ArgoCD's sync cadence adds
  ~30–90s — that part is normal and outside the agent).
- Put the agent log front-and-center: `kubectl logs -f deployment/aiops-agent -n dev`. The audience
  will see `🚨 INCIDENT → 🧠 action → fix → ✅ HEALED` as a live play-by-play.

## Test it — induce each of the 3 failures and watch the agent self-heal

We test on **`qc-service`** (it has ArgoCD `selfHeal` OFF, so a break we induce stays put and the
agent is unambiguously the one that heals it). We break it the GitOps way (a real commit in
`zen-gitops-ajay`, exactly how failures happen in reality); the agent then heals it the GitOps way.

**Before you start**, keep the agent log open in one terminal:
```bash
kubectl logs -f deployment/aiops-agent -n dev
```
In a second terminal, get a local checkout of the gitops repo (you'll edit + commit here):
```bash
gh repo clone ajay-bj/zen-gitops-ajay _zg
cd _zg
```
> The container in every pharma pod is named **`pharma-service`** (Helm chart name), not the
> service name — that's why kubectl `-c` uses `pharma-service`.

---

### TEST 1 — ImagePullBackOff (bad image tag)

**Induce** (point qc-service at a tag that doesn't exist, via gitops → ArgoCD applies it):
```bash
yq -i '.image.tag = "sha-broken999"' envs/dev/values-qc-service.yaml
git commit -am "test: break qc-service image (induce ImagePullBackOff)"
git push
```
**Watch the agent log:** within ~1–2 min the qc-service pod goes `ImagePullBackOff`; the agent logs
`INCIDENT reason=ImagePullBackOff` → `action=ROLLBACK_IMAGE` → a `fix(aiops): rollback ...` commit.

**Verify healed:**
```bash
kubectl get pods -n dev -l app.kubernetes.io/name=qc-service      # 1/1 Running
git pull --ff-only && grep 'tag:' envs/dev/values-qc-service.yaml # back to a good sha
```

---

### TEST 2 — CrashLoopBackOff (process crash)

**Induce** (kill PID 1 in the running container so it crash-loops):
```bash
kubectl exec deployment/qc-service -n dev -c pharma-service -- kill 1
# run 2-3 times if the first restart recovers, to reach CrashLoopBackOff
```
**Watch the agent log:** `INCIDENT reason=CrashLoopBackOff` → `action=RESTART_POD` → `Deleting pod ...`.

**Verify healed:**
```bash
kubectl get pods -n dev -l app.kubernetes.io/name=qc-service      # fresh pod, 1/1 Running
```

---

### TEST 3 — OOMKilled (memory limit too low)

**Induce** (set an absurdly low memory limit via gitops → ArgoCD applies it → pod OOMKilled):
```bash
yq -i '.resources.limits.memory = "16Mi"' envs/dev/values-qc-service.yaml
git commit -am "test: starve qc-service memory (induce OOMKilled)"
git push
```
**Watch the agent log:** `INCIDENT reason=OOMKilled` → `action=BUMP_MEMORY` →
`fix(aiops): raise qc-service memory limit 16Mi -> 512Mi ...` commit.

**Verify healed:**
```bash
kubectl get pods -n dev -l app.kubernetes.io/name=qc-service            # 1/1 Running, no OOM restarts
git pull --ff-only && grep -A3 'limits:' envs/dev/values-qc-service.yaml # memory raised
```

---

### Reset after testing (optional)
The agent already committed working values, so nothing needs undoing. To force a specific known-good
tag back:
```bash
git pull --ff-only
yq -i '.image.tag = "sha-3ceb27b"' envs/dev/values-qc-service.yaml
git commit -am "reset qc-service to known-good" && git push
```

> Live shortcut (no git edit) — works only on `selfHeal`-OFF services (`qc-service`, `auth-service`):
> `kubectl set image deployment/qc-service -n dev pharma-service=304312474711.dkr.ecr.us-east-1.amazonaws.com/qc-service:sha-broken999`
> The agent still heals it via a gitops commit. On the other 7 services, ArgoCD selfHeal would revert
> a live `set image` before the agent acts — so use the gitops method above for those.

---

## Turn healing off/on (safety)
The agent ships with `DRY_RUN=false` (healing on). To observe without acting: set `DRY_RUN=true`
in `k8s/manifests/deployment.yaml`, then `git commit && git push` — ArgoCD applies it. Set back to
`false` to re-enable.

---

## Uninstall
```bash
kubectl delete -f argocd/aiops-agent-app.yaml
kubectl delete -f argocd/aiops-project.yaml
aws iam delete-role-policy --role-name pharma-dev-aiops-role --policy-name aiops-bedrock
aws iam delete-role --role-name pharma-dev-aiops-role
aws secretsmanager delete-secret --name /pharma/dev/aiops-gitops-token \
  --force-delete-without-recovery --region us-east-1
```

---

## If something's off (quick fixes)
| Symptom | Cause | Fix |
|---|---|---|
| `aiops-agent` pod `CreateContainerConfigError` | secret not synced yet | `kubectl get externalsecret aiops-gitops-token -n dev` → must be READY=True (Step 6 must be done) |
| ArgoCD app `Unknown`/`Missing` source | repo private, ArgoCD can't read it | register `zen-aiops` repo creds in ArgoCD (Settings → Repositories) |
| pod runs but Bedrock errors in logs | Nova Pro not enabled in Bedrock | enable model access in AWS Bedrock console, or set `BEDROCK_ENABLED=false` (rule-based healing) |
| agent can't push git fix | PAT wrong/expired | update the secret in Secrets Manager (Step 6), then `kubectl rollout restart deployment/aiops-agent -n dev` |
