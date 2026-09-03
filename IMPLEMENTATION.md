# zen-aiops — Step-by-Step Implementation Guide (DEV only)

`zen-aiops` is a GitOps-aware, self-healing Kubernetes agent. It watches your `dev` namespace,
uses AWS Bedrock (Amazon Nova Pro) to diagnose failing pods, and heals them the GitOps way —
by committing a fix to your gitops repo (which ArgoCD then syncs) or by restarting a pod.

This guide is written to be **forked and reused in your own AWS account**. It plugs into the same
platform the other four hands-on repos build, so every value you need already exists from those:

| This repo depends on | Comes from (earlier hands-on) | Example value |
|---|---|---|
| An EKS cluster + `dev` namespace + IRSA/OIDC | **zen-infra** (Terraform) | cluster `pharma-dev-cluster`, region `us-east-1` |
| ArgoCD + External Secrets Operator installed, 9 apps running | **zen-infra** Stage-2 scripts + **zen-gitops** | namespace `dev` |
| A gitops repo the agent commits fixes to | **zen-gitops** (your fork) | `YOUR_GH_USER/YOUR_GITOPS_REPO` |
| ECR image repos + a `GITOPS_TOKEN` PAT + AWS CI keys | **zen-pharma-backend / -frontend** CI setup | reuse the same PAT and AWS keys |

> **You do NOT need new AWS keys or a new GitHub token.** Reuse the ones you already created for the
> backend/frontend hands-on (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and the `GITOPS_TOKEN` PAT).

---

## 0. Fill in YOUR values first (used throughout this guide)

Everywhere below you'll see these placeholders. Write down your real values once, then substitute them
as you go. To make copy-paste easy, you can also `export` them in your shell (see the two blocks).

| Placeholder | What it is | How to find it |
|---|---|---|
| `<GH_USER>` | your GitHub username/org | your GitHub profile |
| `<AIOPS_REPO>` | your fork of this repo | usually `<GH_USER>/zen-aiops` |
| `<GITOPS_REPO>` | your fork of the gitops repo | e.g. `<GH_USER>/zen-gitops` (from the zen-gitops hands-on) |
| `<ACCOUNT_ID>` | your 12-digit AWS account ID | `aws sts get-caller-identity --query Account --output text` |
| `<REGION>` | AWS region of your cluster/ECR | e.g. `us-east-1` |
| `<CLUSTER>` | your EKS cluster name | e.g. `pharma-dev-cluster` (from zen-infra) |
| `<NAMESPACE>` | namespace the pharma apps run in | `dev` (this guide assumes `dev`) |
| `<AIOPS_ROLE>` | IAM role name to create for the agent | e.g. `pharma-dev-aiops-role` |

**Linux / macOS / Git Bash / WSL — set them once:**
```bash
export GH_USER="your-github-user"
export AIOPS_REPO="$GH_USER/zen-aiops"
export GITOPS_REPO="$GH_USER/zen-gitops"
export ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
export REGION="us-east-1"
export CLUSTER="pharma-dev-cluster"
export NAMESPACE="dev"
export AIOPS_ROLE="pharma-dev-aiops-role"
```

**Windows PowerShell — set them once:**
```powershell
$GH_USER    = "your-github-user"
$AIOPS_REPO = "$GH_USER/zen-aiops"
$GITOPS_REPO= "$GH_USER/zen-gitops"
$ACCOUNT_ID = (aws sts get-caller-identity --query Account --output text)
$REGION     = "us-east-1"
$CLUSTER    = "pharma-dev-cluster"
$NAMESPACE  = "dev"
$AIOPS_ROLE = "pharma-dev-aiops-role"
```

> Throughout the guide, commands use these variables where possible. Where a file must be edited by
> hand, the guide tells you exactly which file and which string to change.

---

## Where to run these commands (READ FIRST)

**Run every command from inside the `zen-aiops` repo folder.** Several steps use relative paths like
`iam/trust-policy.template.json`, `argocd/...`, and `k8s/manifests/...`, which only resolve from the
repo root.

```bash
git clone https://github.com/<GH_USER>/zen-aiops.git   # your fork
cd zen-aiops
ls   # you should see: src  k8s  argocd  iam  Dockerfile  README.md  IMPLEMENTATION.md  AGENT_TESTING.md
```

**Which shell:**
- **Linux / macOS:** normal terminal (bash/zsh). All Linux commands work as written.
- **Windows:** you can use **PowerShell** (every step has a PowerShell block) OR **Git Bash / WSL**
  (use the Linux blocks). Don't mix the two in one step.

**Prerequisites (install first):** `aws` (v2), `kubectl`, `git`, `gh` (GitHub CLI), Docker, and
`yq` (only needed for the demos in `AGENT_TESTING.md`).

---

## 1. Fork + personalize (do this once — a fresh fork will NOT work until you do)

### 1a. Fork the repo
Fork `zen-aiops` to your account on GitHub (the "Fork" button), then clone **your fork** and `cd` in
(see the block above). A fork does not copy secrets and does not auto-run Actions — the next steps fix
both.

### 1b. Enable GitHub Actions on your fork
GitHub disables Actions on new forks by default.
- UI: **Actions** tab → "I understand my workflows, go ahead and enable them", **or** CLI:

**Linux / macOS / Git Bash / WSL:**
```bash
gh api -X PUT repos/$AIOPS_REPO/actions/permissions -F enabled=true -f allowed_actions=all
```
**Windows PowerShell:**
```powershell
gh api -X PUT repos/$AIOPS_REPO/actions/permissions -F enabled=true -f allowed_actions=all
```
> **Important:** `enabled` is a **boolean**, so it must use `-F` (typed field). Using `-f enabled=true`
> sends it as a string and GitHub rejects it with **HTTP 422 "enabled is not a boolean"**.

### 1c. Add your AWS keys as repo secrets (so CI can push the image to ECR)
Secrets are never copied to a fork — set your own. **Reuse the same AWS keys** you used for the
backend/frontend hands-on.

**Linux / macOS / Git Bash / WSL:**
```bash
gh secret set AWS_ACCESS_KEY_ID     --repo $AIOPS_REPO --body "YOUR_AWS_ACCESS_KEY_ID"
gh secret set AWS_SECRET_ACCESS_KEY --repo $AIOPS_REPO --body "YOUR_AWS_SECRET_ACCESS_KEY"
```
**Windows PowerShell:**
```powershell
gh secret set AWS_ACCESS_KEY_ID     --repo $AIOPS_REPO --body "YOUR_AWS_ACCESS_KEY_ID"
gh secret set AWS_SECRET_ACCESS_KEY --repo $AIOPS_REPO --body "YOUR_AWS_SECRET_ACCESS_KEY"
```

### 1d. Replace the baked-in account/repo values
This repo ships wired to the author's account. Replace three things across the tracked files:

| Find | Replace with | Appears in |
|---|---|---|
| `304312474711` | your `<ACCOUNT_ID>` | `k8s/manifests/rbac.yaml`, `k8s/manifests/deployment.yaml`, `iam/trust-policy.template.json` |
| `ajay-bj/zen-gitops-ajay` | your `<GITOPS_REPO>` | `k8s/manifests/deployment.yaml` (env `GITOPS_REPO`) |
| `ajay-bj/zen-aiops` | your `<AIOPS_REPO>` | `argocd/aiops-agent-app.yaml` (`repoURL`) |

Also confirm the region/cluster/namespace in `k8s/manifests/deployment.yaml` and
`iam/trust-policy.template.json` match yours (`<REGION>`, `<CLUSTER>`, `<NAMESPACE>`). Defaults are
`us-east-1`, `pharma-dev-cluster`, `dev`.

**Linux / macOS / Git Bash / WSL — do the replacements automatically:**
```bash
grep -rl '304312474711'        . | xargs sed -i "s/304312474711/$ACCOUNT_ID/g"
grep -rl 'ajay-bj/zen-gitops-ajay' . | xargs sed -i "s#ajay-bj/zen-gitops-ajay#$GITOPS_REPO#g"
grep -rl 'ajay-bj/zen-aiops'   . | xargs sed -i "s#ajay-bj/zen-aiops#$AIOPS_REPO#g"
git commit -am "chore: personalize zen-aiops for my account" && git push
```
**Windows PowerShell — do the replacements automatically:**
```powershell
Get-ChildItem -Recurse -File | ForEach-Object {
  (Get-Content $_.FullName) `
    -replace '304312474711', $ACCOUNT_ID `
    -replace 'ajay-bj/zen-gitops-ajay', $GITOPS_REPO `
    -replace 'ajay-bj/zen-aiops', $AIOPS_REPO |
  Set-Content $_.FullName
}
git commit -am "chore: personalize zen-aiops for my account"
git push
```
> **How CI/CD triggers (in short):** `.github/workflows/build.yml` runs on **push to `main`** (changes
> under `src/**`, `Dockerfile`, `requirements.txt`, or the workflow) **or** a manual **Run workflow**
> (`workflow_dispatch`). It builds the agent image and pushes it to **your** ECR. It does **not** deploy
> to the cluster — ArgoCD does that (Step 6). So the flow is: **push code → CI builds image → ECR;
> ArgoCD deploys the manifests.** The push you just did in 1d already triggers a build.

---

## Prerequisites — confirm the platform is up (from the earlier hands-on)
- The pharma platform is deployed: apps Running in `<NAMESPACE>`, with **ArgoCD** and
  **External Secrets Operator (ESO)** installed (done in the zen-infra Stage-2 + zen-gitops hands-on).
- You can reach AWS and the cluster:

**Linux / macOS / Git Bash / WSL:**
```bash
aws sts get-caller-identity
aws eks update-kubeconfig --region $REGION --name $CLUSTER
kubectl get pods -n $NAMESPACE
```
**Windows PowerShell:**
```powershell
aws sts get-caller-identity
aws eks update-kubeconfig --region $REGION --name $CLUSTER
kubectl get pods -n $NAMESPACE
```

---

## Step 2 — Build & push the agent image (trigger CI, then wait)
If your push in 1d didn't already start a build, trigger it, then confirm the image reached ECR.

**Linux / macOS / Git Bash / WSL:**
```bash
gh workflow run build.yml --repo $AIOPS_REPO
# wait ~2 min, then confirm the image landed:
aws ecr describe-images --repository-name aiops-agent --region $REGION \
  --query "imageDetails[].imageTags" --output text
```
**Windows PowerShell:**
```powershell
gh workflow run build.yml --repo $AIOPS_REPO
Start-Sleep -Seconds 120
aws ecr describe-images --repository-name aiops-agent --region $REGION `
  --query "imageDetails[].imageTags" --output text
```
You should see `latest` (and a `sha-...` tag). If the ECR repo didn't exist, CI created it.

---

## Step 3 — Create the agent IAM role (IRSA)
The agent pod assumes this role via IRSA (no static keys in the pod). The inline policy
(`iam/bedrock-policy.json`) grants exactly what the agent needs:
- `bedrock:InvokeModel*` — root-cause analysis with Amazon Nova Pro.
- `ecr:DescribeImages` / `ecr:ListImages` / `ecr:DescribeRepositories` — so the ImagePullBackOff heal
  only rolls back to an image tag that **still exists in ECR** (a tag pruned by the ECR lifecycle
  policy would just fail to pull again).

> `iam/trust-policy.template.json` is a **plain IAM policy document** — do **not** add a `_comment`
> field to it; AWS rejects unknown fields with `MalformedPolicyDocument`. The `<OIDC_ID>` placeholder
> in it is filled automatically by the commands below.

**Linux / macOS / Git Bash / WSL:**
```bash
OIDC=$(aws eks describe-cluster --name $CLUSTER --region $REGION \
  --query "cluster.identity.oidc.issuer" --output text | awk -F'/id/' '{print $2}') && \
sed "s/<OIDC_ID>/$OIDC/g" iam/trust-policy.template.json > /tmp/aiops-trust.json && \
aws iam create-role --role-name $AIOPS_ROLE \
  --assume-role-policy-document file:///tmp/aiops-trust.json && \
aws iam put-role-policy --role-name $AIOPS_ROLE \
  --policy-name aiops-bedrock --policy-document file://iam/bedrock-policy.json && \
echo "IAM role $AIOPS_ROLE ready"
```
**Windows PowerShell:**
```powershell
$OIDC = (aws eks describe-cluster --name $CLUSTER --region $REGION `
  --query "cluster.identity.oidc.issuer" --output text) -split '/id/' | Select-Object -Last 1
(Get-Content iam/trust-policy.template.json) -replace '<OIDC_ID>', $OIDC | Set-Content "$env:TEMP\aiops-trust.json"
aws iam create-role --role-name $AIOPS_ROLE `
  --assume-role-policy-document "file://$env:TEMP\aiops-trust.json"
aws iam put-role-policy --role-name $AIOPS_ROLE `
  --policy-name aiops-bedrock --policy-document file://iam/bedrock-policy.json
Write-Host "IAM role $AIOPS_ROLE ready"
```
> The service account the agent uses (`aiops-agent` in `<NAMESPACE>`) is annotated with this role's ARN
> in `k8s/manifests/rbac.yaml`. If you changed `<AIOPS_ROLE>` from the default, update that annotation
> too (it embeds the role name and `<ACCOUNT_ID>`).

---

## Step 4 — Put the GitOps PAT in AWS Secrets Manager
The agent commits fixes to your gitops repo, so it needs a GitHub PAT with **`contents:write`** on
`<GITOPS_REPO>`. **Reuse the same `GITOPS_TOKEN` PAT** you created for the backend/frontend CI — it
already has the right scope. ESO reads this secret and injects it into the agent pod at runtime.

**Linux / macOS / Git Bash / WSL:**
```bash
aws secretsmanager create-secret \
  --name /pharma/$NAMESPACE/aiops-gitops-token \
  --secret-string '{"token":"YOUR_GITOPS_PAT"}' \
  --region $REGION
```
**Windows PowerShell (note the escaped quotes):**
```powershell
aws secretsmanager create-secret `
  --name /pharma/$NAMESPACE/aiops-gitops-token `
  --secret-string '{\"token\":\"YOUR_GITOPS_PAT\"}' `
  --region $REGION
```
> If the secret already exists, use `put-secret-value` instead of `create-secret`. The CI copy of the
> token lives in GitHub secrets; this pod copy must live in Secrets Manager — same value, two consumers.
> The path `/pharma/<NAMESPACE>/aiops-gitops-token` must match `k8s/manifests/external-secret.yaml`.

---

## Step 5 — Deploy via ArgoCD (one-time bootstrap; everything after auto-syncs)
This is the only manual `kubectl` — one command, run once. It registers the ArgoCD project + app
(project first, then app). After this, ArgoCD owns the agent (RBAC + ExternalSecret + Deployment from
`k8s/manifests/`) and every future change you push to this repo auto-syncs.

```bash
kubectl apply -f argocd/aiops-project.yaml -f argocd/aiops-agent-app.yaml
```
> If your fork of `zen-aiops` is **private**, register its repo credentials in ArgoCD first
> (ArgoCD UI → Settings → Repositories), or ArgoCD can't read the manifests.

---

## Step 6 — Verify it's healthy

**Linux / macOS / Git Bash / WSL:**
```bash
kubectl get application aiops-agent-dev -n argocd          # SYNCED / HEALTHY
kubectl get externalsecret aiops-gitops-token -n $NAMESPACE # READY=True
kubectl get pods -n $NAMESPACE -l app=aiops-agent          # 1/1 Running
kubectl logs -f deployment/aiops-agent -n $NAMESPACE       # startup banner
```
**Windows PowerShell:**
```powershell
kubectl get application aiops-agent-dev -n argocd
kubectl get externalsecret aiops-gitops-token -n $NAMESPACE
kubectl get pods -n $NAMESPACE -l app=aiops-agent
kubectl logs -f deployment/aiops-agent -n $NAMESPACE
```
The startup banner should show your namespace, interval, `bedrock=True`, and your `GITOPS_REPO`.

**Done.** The agent now watches your `<NAMESPACE>` pods and auto-heals them.

---

## Testing the self-heal
To see the agent detect and auto-heal failures, follow **[`AGENT_TESTING.md`](AGENT_TESTING.md)** —
it has two demos (CrashLoopBackOff and ImagePullBackOff), each with the exact commands to induce the
failure and the real agent log output showing the heal.

---

## Turn healing off/on (safety)
The agent ships with `DRY_RUN=false` (healing on). To observe decisions without acting, set
`DRY_RUN=true` in `k8s/manifests/deployment.yaml`, then `git commit && git push` — ArgoCD applies it.
Set back to `false` to re-enable.

---

## Uninstall
Replace the variables (or run in the shell where you `export`ed them).
```bash
kubectl delete -f argocd/aiops-agent-app.yaml
kubectl delete -f argocd/aiops-project.yaml
aws iam delete-role-policy --role-name $AIOPS_ROLE --policy-name aiops-bedrock
aws iam delete-role --role-name $AIOPS_ROLE
aws secretsmanager delete-secret --name /pharma/$NAMESPACE/aiops-gitops-token \
  --force-delete-without-recovery --region $REGION
```

---

## If something's off (quick fixes)
| Symptom | Cause | Fix |
|---|---|---|
| Actions won't enable / HTTP 422 "not a boolean" | used `-f enabled=true` | use `-F enabled=true` (typed boolean) — Step 1b |
| `aiops-agent` pod `CreateContainerConfigError` | secret not synced yet | `kubectl get externalsecret aiops-gitops-token -n <NAMESPACE>` must be READY=True (Step 4) |
| ArgoCD app `Unknown`/`Missing` source | your fork is private, ArgoCD can't read it | register the `zen-aiops` repo creds in ArgoCD (Settings → Repositories) |
| `create-role` fails `MalformedPolicyDocument: Unknown field _comment` | a comment field was added to the trust policy | remove any `_comment` from `iam/trust-policy.template.json` (Step 3) |
| pod runs but Bedrock errors in logs | Nova Pro not enabled in Bedrock | enable model access in the AWS Bedrock console, or set `BEDROCK_ENABLED=false` (rule-based healing) |
| agent can't push git fix | PAT wrong/expired/scope | update the secret in Secrets Manager (Step 4), then `kubectl rollout restart deployment/aiops-agent -n <NAMESPACE>` |
| ImagePullBackOff heal keeps flipping tags / `AccessDenied` on ECR in logs | agent role missing ECR read | re-run the `put-role-policy` in Step 3 (it includes `ecr:DescribeImages` etc.), then `kubectl rollout restart deployment/aiops-agent -n <NAMESPACE>` |
| ArgoCD app error: `no matches for kind ExternalSecret ... v1beta1` | your ESO serves a different API version | `k8s/manifests/external-secret.yaml` uses `external-secrets.io/v1`. Check yours with `kubectl get crd externalsecrets.external-secrets.io -o jsonpath='{.spec.versions[*].name}'` and match it |
