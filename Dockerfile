# Zen Pharma AIOps Self-Healing Agent (GitOps-aware, dev)
FROM python:3.12-slim

# git is required for the GitOps remediation path (clone/commit/push to zen-gitops)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root runtime user with an EXPLICIT uid/gid 1000 so it matches the
# Deployment's securityContext.runAsUser: 1000. This is essential: the agent
# clones the gitops repo into /app/_gitops_work at runtime, so the running UID
# MUST own /app (otherwise git clone/config/push fails with permission denied).
RUN groupadd -g 1000 pharma \
    && useradd -u 1000 -g 1000 -m -d /app -s /usr/sbin/nologin pharma

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Ensure the runtime user owns /app (HOME) so git config + the working checkout
# under /app/_gitops_work are writable.
RUN chown -R 1000:1000 /app

# HOME must be writable for `git config`.
ENV HOME=/app
USER 1000

EXPOSE 8000

CMD ["python", "-m", "src.main"]
