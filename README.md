# zen-aiops — GitOps-Aware Kubernetes Self-Healing Agent

An AI-powered self-healing agent that runs **inside the existing `pharma-dev-cluster`**,
watches the 9 Zen Pharma services in the `dev` namespace, and automatically heals
`ImagePullBackOff`, `OOMKilled`, and `CrashLoopBackOff` back to a healthy state — the
**GitOps way**, cooperating with ArgoCD instead of fighting it.

Production model, matching the rest of the platform:
- **Deployed by ArgoCD** as its own Application (no manual `kubectl apply` of the agent).
- **Secrets via External Secrets Operator** (AWS Secrets Manager → K8s Secret) — nothing in git.
- **AWS auth via IRSA** (no static keys in the pod).
- **DEV only.**

---

## What it does

```
Pod fails in dev  →  agent detects (≤30s)  →  gathers logs+events+image history
                  →  AWS Bedrock (Nova Pro) picks a remediation
                  →  executor applies the GitOps-correct fix  →  verifies recovery
```

| Failure | Root cause | Remediation (GitOps-correct) |
|---|---|---|
| `ImagePullBackOff` / `ErrImagePull` | bad/missing image tag | **git commit** reverting `image.tag` to the previous known-good in `zen-gitops-ajay/envs/dev/values-<svc>.yaml` → ArgoCD syncs |
| `OOMKilled` | memory limit too low | **git commit** raising `resources.limits.memory` in the values file → ArgoCD syncs |
| `CrashLoopBackOff` | transient app crash | **delete the pod** — the Deployment recreates it (ArgoCD doesn't manage individual pods, so no drift) |
| `CreateContainerConfigError` | missing config/secret | **ALERT** only (no safe auto-fix) |

### Why GitOps-aware matters (the core design decision)

The pharma platform is deployed by **ArgoCD** from `zen-gitops-ajay`. Most dev apps have
`syncPolicy.automated.selfHeal: true`. If the agent patched a live Deployment (image or
memory), **ArgoCD would immediately revert it** to match git. So durable fixes must be made in
git. Only pod deletion is safe imperatively, because ArgoCD reconciles Deployments, not the
individual pods they create.

### How a fix actually flows (through ArgoCD)

The agent never patches the live cluster for image/memory issues. It does what a human operator
would do — **commit the fix to `zen-gitops-ajay`; ArgoCD applies it.** Same path CI uses to deploy.

**ImagePullBackOff** (durable, via ArgoCD):
```
pod ImagePullBackOff
  → agent finds previous good tag in git history of envs/dev/values-<svc>.yaml
  → agent commits & pushes:  image.tag: sha-broken → sha-good   (to zen-gitops-ajay)
  → ArgoCD detects the commit → syncs → Deployment updated → pod pulls good image → Running
  commit msg: "fix(aiops): rollback <svc> image sha-broken -> sha-good (auto-heal ImagePullBackOff)"
```

**OOMKilled** (durable, via ArgoCD):
```
pod OOMKilled
  → agent commits & pushes:  resources.limits.memory: 16Mi → 512Mi   (to zen-gitops-ajay)
  → ArgoCD syncs → Deployment gets new memory limit → pod stops OOMing → Running
  commit msg: "fix(aiops): raise <svc> memory limit 16Mi -> 512Mi (auto-heal OOMKilled)"
```

**CrashLoopBackOff** (transient — NOT a git change, and this does NOT bypass ArgoCD):
```
pod CrashLoopBackOff
  → agent deletes the pod (kubectl delete pod)
  → the Deployment (still owned by ArgoCD) recreates the pod → Running
```
> Why pod-delete is safe here: ArgoCD reconciles the *Deployment*, not the individual *pods* it
> spawns. Deleting a pod is not "drift" — the ReplicaSet immediately recreates it. Nothing in git
> is wrong for a transient crash, so no commit is needed. A human would do the same thing.

You will see the agent's commits in `zen-gitops-ajay` and the app briefly go `OutOfSync → Synced`
in the ArgoCD UI — fully auditable and reversible, exactly like a CI-driven deploy.

**Why not `kubectl patch` the image/memory directly?** 7 of the 9 dev apps have `selfHeal: true`,
so ArgoCD would revert a live patch within seconds and the pod would fail again. Durable fixes
must live in git.

```
                         ┌─────────────────────────────────────────────┐
                         │  pharma-dev-cluster · namespace: dev         │
   ┌──────────────┐      │  ┌────────────┐   watches   ┌────────────┐   │
   │ AWS Bedrock  │◀─────┼──│ aiops-agent│────────────▶│ 9 pharma   │   │
   │ (Nova Pro)   │ IRSA │  │  (this pod)│   pod status │  services  │   │
   └──────────────┘      │  └─────┬──────┘             └────────────┘   │
                         └────────┼──────────────────────────────────────┘
                                  │ git commit (image/memory fix)
                                  ▼
                    ┌───────────────────────────┐   ArgoCD    ┌──────────────┐
                    │ github.com/ajay-bj/        │───syncs────▶│ dev namespace │
                    │ zen-gitops-ajay (envs/dev) │             │  (healed)     │
                    └───────────────────────────┘             └──────────────┘

  The agent ITSELF is also deployed by ArgoCD (Application `aiops-agent-dev`, project `aiops`)
  from THIS repo's k8s/manifests. Secrets arrive via ESO. Nothing is applied by hand.
```

---

## Tech stack

| Component | Technology |
|---|---|
| Agent | Python 3.12 · `kubernetes` client · `boto3` · `ruamel.yaml` |
| AI model | Amazon Nova Pro via Bedrock Converse API (rule-based fallback if Bedrock is down) |
| AWS auth | IRSA (OIDC) — no static keys in the pod |
| Git auth | GitHub PAT delivered by ESO from AWS Secrets Manager (never in git) |
| K8s auth | RBAC — read pods/logs/events/deployments; delete pods |
| Deploy | ArgoCD Application (`aiops-agent-dev`) → this repo's `k8s/manifests` |
| Cluster | Existing `pharma-dev-cluster` (EKS 1.35), `dev` namespace |
| CI/CD | GitHub Actions → ECR (`aiops-agent`) |

---

## How credentials are handled (no secrets in git or the image)

Three separate credentials, each handled the production way — matching the rest of the platform:

| Credential | Used for | How it's provided | Where it lives |
|---|---|---|---|
| **AWS creds (Bedrock)** | agent → AWS Bedrock | **IRSA** — SA `dev/aiops-agent` assumes IAM role `pharma-dev-aiops-role` via OIDC; STS issues short-lived creds (~1h, auto-rotated). `boto3` uses them automatically. | Nowhere persistent — no keys in the pod or image |
| **GitHub PAT (GitOps push)** | agent → commit fixes to `zen-gitops-ajay` | **External Secrets Operator** syncs it from AWS Secrets Manager (`/pharma/dev/aiops-gitops-token`) into K8s Secret `aiops-gitops-token`; Deployment reads it as env `GITOPS_TOKEN` via `secretKeyRef`. Same pattern as pharma `jwt-secret`/`db-credentials`. | AWS Secrets Manager only |
| **AWS keys (CI → ECR)** | GitHub Actions build → push image | GitHub Actions **repo secrets** `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`, used only inside CI. | GitHub encrypted secrets |

**Reuse what you already have — no new credentials needed:**
- **GitHub PAT:** reuse the same `GITOPS_TOKEN` you created for the backend/frontend CI (it already
  has `contents:write` on `zen-gitops-ajay`). Just store that same value in Secrets Manager so ESO
  can give it to the agent pod.
- **AWS keys for CI:** reuse the same key pair you use for `zen-infra` CI. No new IAM user.
- **Bedrock access:** the only genuinely new thing is the IRSA role `pharma-dev-aiops-role`
  (one-time CLI); there's no existing Bedrock role to reuse.
- **ESO/Secrets Manager plumbing:** fully reused — the `aws-secrets-manager` ClusterSecretStore and
  `pharma-dev-eso-role` already exist and read `/pharma/*`, so the new ExternalSecret needs no setup.

Key points:
- The GitHub token and AWS access are **never** committed to git or baked into the image.
- The PAT flows: `AWS Secrets Manager → ESO → K8s Secret → env var` — you never `kubectl create secret` by hand.
- The agent's AWS access to Bedrock uses **no static keys** (IRSA/OIDC), the same keyless model as ESO and ArgoCD in the pharma platform.

---

## Project structure

```
zen-aiops/
├── src/                        # agent code (see modules below)
│   ├── main.py                 # watch loop, cooldown, orchestration, health :8000
│   ├── config.py               # env-driven config + safety flags
│   ├── services.py             # registry: deployment -> values file -> ECR repo (allow-list)
│   ├── k8s_client.py           # connect, detect, gather context, resolve deploy, delete pod
│   ├── bedrock.py              # Nova Pro RCA (Converse) + rule-based fallback
│   ├── gitops.py               # clone/commit/push image rollback & memory bump (ruamel.yaml)
│   └── executor.py             # route action -> gitops commit / pod delete + guardrails
├── k8s/manifests/              # <-- ArgoCD applies THIS directory
│   ├── rbac.yaml               # ServiceAccount (IRSA) + ClusterRole + Binding
│   ├── external-secret.yaml    # ESO ExternalSecret -> aiops-gitops-token
│   └── deployment.yaml         # agent Deployment (dev namespace)
├── argocd/
│   ├── aiops-project.yaml       # dedicated 'aiops' AppProject (sourceRepos = this repo)
│   └── aiops-agent-app.yaml     # ArgoCD Application (project aiops, path k8s/manifests)
├── iam/
│   ├── bedrock-policy.json          # IAM policy: bedrock:InvokeModel*
│   └── trust-policy.template.json   # IRSA trust policy (parameterized <OIDC_ID>)
├── .github/workflows/build.yml # CI: build → push to ECR aiops-agent
├── Dockerfile · requirements.txt · README.md
```

---

## One-time setup (bootstrap that ArgoCD/ESO cannot self-create)

These are AWS-level and secret-material steps — the same kind of one-time bootstrap the pharma
platform itself needed. Everything after this is GitOps.

Prereqs: pharma platform deployed (9 apps in `dev`), ESO + ArgoCD installed (they are — the
platform uses them), `aws`/`kubectl`/`gh`/Docker available. Account `304312474711`, `us-east-1`.

### 1. Build & push the agent image
Push this repo to `ajay-bj/zen-aiops` with repo secrets `AWS_ACCESS_KEY_ID` /
`AWS_SECRET_ACCESS_KEY`; CI (`.github/workflows/build.yml`) builds and pushes
`aiops-agent:latest` (+`:sha-<7>`) to ECR and creates the ECR repo if missing.

### 2. Create the Bedrock IAM role (IRSA) — one-time CLI
```bash
aws eks update-kubeconfig --region us-east-1 --name pharma-dev-cluster

# a. Get the cluster OIDC id
OIDC=$(aws eks describe-cluster --name pharma-dev-cluster --region us-east-1 \
  --query "cluster.identity.oidc.issuer" --output text | awk -F'/id/' '{print $2}')

# b. Render the trust policy for dev:aiops-agent
sed "s/<OIDC_ID>/$OIDC/g" iam/trust-policy.template.json > /tmp/aiops-trust.json

# c. Create the role + attach the Bedrock policy
aws iam create-role --role-name pharma-dev-aiops-role \
  --assume-role-policy-document file:///tmp/aiops-trust.json
aws iam put-role-policy --role-name pharma-dev-aiops-role \
  --policy-name aiops-bedrock --policy-document file://iam/bedrock-policy.json
```
(The ServiceAccount in `k8s/manifests/rbac.yaml` is already annotated with this role ARN.)

### 3. Put the GitOps PAT in AWS Secrets Manager — one-time
Create a GitHub PAT with `contents:write` on `ajay-bj/zen-gitops-ajay`, then:
```bash
aws secretsmanager create-secret \
  --name /pharma/dev/aiops-gitops-token \
  --secret-string '{"token":"ghp_your_pat_here"}' \
  --region us-east-1
```
ESO (via the existing `aws-secrets-manager` ClusterSecretStore + `pharma-dev-eso-role`, which can
read `/pharma/*`) will sync this into the K8s Secret `aiops-gitops-token` — no manual secret.

### 4. Register the ArgoCD app (one-time apply; deploys everything else)
```bash
kubectl apply -f argocd/aiops-project.yaml     # 'aiops' AppProject
kubectl apply -f argocd/aiops-agent-app.yaml   # Application -> k8s/manifests
```
> If `zen-aiops` is a **private** repo, first register its read credentials in ArgoCD
> (Settings → Repositories) so ArgoCD can pull the manifests.

That's it. ArgoCD now syncs `k8s/manifests/` (RBAC + ExternalSecret + Deployment). From here on,
**any change you push to `k8s/manifests/` auto-deploys** — no `kubectl apply`.

### 5. Watch it come up
```bash
kubectl get application aiops-agent-dev -n argocd     # Synced + Healthy
kubectl get pods -n dev -l app=aiops-agent            # 1/1 Running
kubectl logs -f deployment/aiops-agent -n dev
```

> **Start safe:** `k8s/manifests/deployment.yaml` ships `DRY_RUN=false`. To observe decisions
> without acting first, set `DRY_RUN=true`, commit+push (ArgoCD applies it), verify the logs,
> then set back to `false`.

---

## Demos — induce failures, watch it heal

> **Full copy-paste test steps (induce + verify each scenario) are in
> [`IMPLEMENTATION.md`](./IMPLEMENTATION.md) → "Test it".** The summary below explains what happens.

Keep the agent log open, then break `qc-service` (its ArgoCD selfHeal is OFF, so the break is
unambiguously healed by the agent):
```bash
kubectl logs -f deployment/aiops-agent -n dev
```

- **Demo 1 — ImagePullBackOff:** set `image.tag: sha-broken999` in
  `zen-gitops-ajay/envs/dev/values-qc-service.yaml`, commit+push → ArgoCD applies the bad tag →
  agent detects it, commits an image rollback to the previous good tag → ArgoCD syncs → Running.
- **Demo 2 — OOMKilled:** set `resources.limits.memory: 16Mi` in the same file, commit+push →
  pod OOMKilled → agent commits a memory bump (→ 512Mi floor) → ArgoCD syncs → stops OOMing.
- **Demo 3 — CrashLoopBackOff:** `kubectl exec deployment/qc-service -n dev -c pharma-service -- kill 1`
  → agent detects the crash loop, deletes the pod → Deployment recreates it → healthy.

> The container inside every pharma pod is **`pharma-service`** (Helm chart name), not the service
> name — hence `-c pharma-service` in kubectl commands.

---

## Safety guardrails
- **Allow-list:** only the 9 known pharma deployments (`src/services.py`) are ever touched.
- **DRY_RUN:** analyze without acting.
- **Action cross-check:** executor overrides the model if its action disagrees with the observed
  failure reason (never deletes a pod for an `ImagePullBackOff`, etc.).
- **Per-pod cooldown** (default 5 min) prevents fix storms.
- **Bounded OOM bumps** (multiplicative, capped).
- **Never patches Deployments live** (would fight ArgoCD); durable fixes go through git.
- **No secrets in image or git:** AWS via IRSA, GitOps token via ESO from Secrets Manager.

---

## Cleanup
```bash
kubectl delete -f argocd/aiops-agent-app.yaml
kubectl delete -f argocd/aiops-project.yaml
aws iam delete-role-policy --role-name pharma-dev-aiops-role --policy-name aiops-bedrock
aws iam delete-role --role-name pharma-dev-aiops-role
aws secretsmanager delete-secret --name /pharma/dev/aiops-gitops-token --force-delete-without-recovery --region us-east-1
# (optional) aws ecr delete-repository --repository-name aiops-agent --region us-east-1 --force
```

---

## Scope
**DEV only.** Watches and heals the `dev` namespace on `pharma-dev-cluster`. Does not touch qa/prod.
