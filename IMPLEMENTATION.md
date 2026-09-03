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

## Using this in YOUR account (fork-and-use) — READ THIS FIRST

This repo is meant to be **forked**. After you fork it to your GitHub account, do these fork-specific
things (a fresh fork will NOT build or work until you do):

**1. Enable GitHub Actions on your fork** (GitHub disables Actions on new forks by default):
- Actions tab → "I understand my workflows, go ahead and enable them", or:
```bash
gh api -X PUT repos/<YOUR_GH_USER>/zen-aiops/actions/permissions -F enabled=true -f allowed_actions=all
```
> Note: `enabled` is a **boolean**, so it must use `-F` (typed field). Using `-f enabled=true`
> sends a string and GitHub rejects it with HTTP 422 "enabled is not a boolean".

**2. Add your AWS secrets** (secrets are NEVER copied to a fork — set your own):
```bash
gh secret set AWS_ACCESS_KEY_ID     --repo <YOUR_GH_USER>/zen-aiops --body "YOUR_AWS_ACCESS_KEY_ID"
gh secret set AWS_SECRET_ACCESS_KEY --repo <YOUR_GH_USER>/zen-aiops --body "YOUR_AWS_SECRET_ACCESS_KEY"
```

**3. Replace the account-specific values** (this repo is wired to a specific account/gitops repo):
- `304312474711` → **your** 12-digit AWS account ID (in `k8s/manifests/rbac.yaml`, `deployment.yaml`, `iam/trust-policy.template.json`)
- `ajay-bj/zen-gitops-ajay` → **your** gitops repo (in `k8s/manifests/deployment.yaml` env `GITOPS_REPO`)
- `argocd/aiops-agent-app.yaml` `repoURL` → **your** fork URL of `zen-aiops`
```bash
# from the repo root, replace across all files (Linux/macOS/Git Bash):
grep -rl '304312474711' . | xargs sed -i 's/304312474711/YOUR_ACCOUNT_ID/g'
grep -rl 'ajay-bj/zen-gitops-ajay' . | xargs sed -i 's#ajay-bj/zen-gitops-ajay#YOUR_GH_USER/YOUR_GITOPS_REPO#g'
grep -rl 'ajay-bj/zen-aiops' . | xargs sed -i 's#ajay-bj/zen-aiops#YOUR_GH_USER/zen-aiops#g'
git commit -am "chore: personalize for my account" && git push
```

**4. Trigger the CI/CD** (forking does NOT auto-run Actions — you must trigger it):
- The push in step 3 triggers it automatically (it touches tracked files), OR
- Run it manually anytime:
```bash
gh workflow run build.yml --repo <YOUR_GH_USER>/zen-aiops
```

> **How the CI/CD triggers, in short:** the workflow (`.github/workflows/build.yml`) runs on **push to
> `main`** (changes under `src/**`, `Dockerfile`, `requirements.txt`, or the workflow) **or** manual
> **Run workflow** (`workflow_dispatch`). It builds the agent image and pushes it to your ECR. It does
> NOT deploy to the cluster — ArgoCD does that (Step 7). So: **push code → CI builds image → ECR;
> ArgoCD deploys the manifests.**

After the fork setup above, follow Steps 1–8 (Step 1 is only for the original author; forkers skip it).

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
gh api -X PUT repos/ajay-bj/zen-aiops/actions/permissions -F enabled=true -f allowed_actions=all
```
> `enabled` is a boolean — use `-F` (not `-f`), or GitHub returns HTTP 422 "not a boolean".

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

## Step 5 — Create the agent IAM role (IRSA) — one block, copy-paste
(Run from the `zen-aiops` folder — it reads `iam/*.json`.)

> The inline policy (`iam/bedrock-policy.json`) grants **two** things the agent needs:
> - `bedrock:InvokeModel*` — root-cause analysis via Amazon Nova Pro.
> - `ecr:DescribeImages` / `ecr:ListImages` / `ecr:DescribeRepositories` — so the ImagePullBackOff
>   heal only rolls back to an image tag that **actually exists in ECR** (a tag pruned by the ECR
>   lifecycle policy would just fail to pull again). Both are applied by the single command below.
>
> `iam/trust-policy.template.json` is a **plain IAM policy document** — do not add a `_comment`
> field to it; AWS rejects unknown fields with `MalformedPolicyDocument`.

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

## Step 7 — Deploy via ArgoCD (one-time bootstrap; everything else auto-syncs)
This is the ONLY manual `kubectl` — one command, run once. It registers the ArgoCD project + app;
`kubectl` applies the files in the order given (project first, then app). After this, ArgoCD owns
the agent (RBAC + ExternalSecret + Deployment from `k8s/manifests/`) and every future change to this
repo auto-syncs — no more `kubectl`. (Same one-time bootstrap the pharma apps used.)
```bash
kubectl apply -f argocd/aiops-project.yaml -f argocd/aiops-agent-app.yaml
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
- The agent ships with **demo timings**: scans every **15s**, verifies after **20s**, per-pod
  cooldown **60s**, and a per-deployment **image-rollback cooldown of 150s**
  (`ROLLBACK_COOLDOWN_SECONDS` in `k8s/manifests/deployment.yaml`). For production raise these to
  roughly 30 / 45 / 300 / 300.
- **Why the rollback cooldown matters:** after the agent rolls a service's image back, the old
  ReplicaSet's failing pod can linger in `ImagePullBackOff` for a bit while ArgoCD converges. The
  cooldown makes the agent heal **once** and then wait for ArgoCD, instead of thrashing between tags.
  In the log you'll see one `fix(aiops): rollback ...` then
  `image rolled back recently; waiting for ArgoCD to converge (cooldown)` — that's expected and
  correct, not a stall.
- **Best first demo = CrashLoopBackOff (Test 2):** the agent deletes the pod directly and the
  Deployment recreates it in seconds — no ArgoCD wait, so the heal is near-instant on screen.
- The image demo goes through git → ArgoCD, so allow ~1–2 min total (ArgoCD's sync cadence adds
  ~30–90s — that part is normal and outside the agent).
- Put the agent log front-and-center: `kubectl logs -f deployment/aiops-agent -n dev`. The audience
  will see `🚨 INCIDENT → 🧠 action → fix → ✅ HEALED` as a live play-by-play.

## Test it — induce each failure and watch the agent self-heal
> Full output-annotated walkthrough: see **`AGENT_TESTING.md`**.

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
`INCIDENT reason=ImagePullBackOff` → `action=ROLLBACK_IMAGE` → **one** `fix(aiops): rollback ...`
commit to a tag that exists in ECR. It then enters the rollback cooldown and waits for ArgoCD to
converge (you may see a `waiting for ArgoCD to converge (cooldown)` line — that's normal).

**Verify healed:**
```bash
kubectl get pods -n dev -l app.kubernetes.io/name=qc-service      # 1/1 Running
git pull --ff-only && grep 'tag:' envs/dev/values-qc-service.yaml # back to a good sha
```

> If your gitops repo has old image tags in its history that ECR has since pruned, the agent skips
> them and rolls back to the most recent tag that still exists in ECR — so the heal always lands on
> a pullable image. (This needs the ECR read permission from Step 5.)

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

> A full, output-annotated walkthrough of both demos lives in **`AGENT_TESTING.md`**.

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
| ImagePullBackOff heal keeps flipping tags / `AccessDenied` on ECR in logs | agent role missing ECR read | re-run Step 5 (the `put-role-policy` now includes `ecr:DescribeImages` etc.), then `kubectl rollout restart deployment/aiops-agent -n dev` |
| ArgoCD app error: `no matches for kind ExternalSecret ... v1beta1` | your ESO serves a different API version | `k8s/manifests/external-secret.yaml` uses `external-secrets.io/v1` (what this cluster's ESO serves). If yours differs, run `kubectl get crd externalsecrets.external-secrets.io -o jsonpath='{.spec.versions[*].name}'` and match it |
