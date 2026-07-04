# qh-deploy-portal

Agent-driven "bring your own image → get a URL" for internal tools on **qh-dev-control**.
A person describes a tool in **Slack**; a hosted agent scaffolds it, Fleet deploys it, and a
TTL reaper cleans it up. **Nobody's laptop is involved.**

This repo is **the machine** — the golden template, the scaffolder, the Slack agent, and the
reaper. It does **not** hold tool source or deployed manifests:

- Tool **source** lives in the author's own repo; they build + push an **image** (linux/amd64) to
  the qh-mgmt Artifact Registry (`us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/<repo>:<tag>`).
- Tool **manifests** (generated overlays) are written into **`qh-deployment`** at
  `qh-control/base/tools/<name>/`, where Fleet already watches and deploys to dev-control.
- **`qh-platform` is never touched.**

## How it works

```
person (Slack DM/@mention)
   ─► deploy-agent  (in-cluster, Socket Mode, Anthropic API direct)
        extracts a spec, confirms it, runs scaffold.py
   ─► writes qh-control/base/tools/<name>/ in qh-deployment ─► commit + push
   ─► Fleet deploys to qh-dev-control ─► cert-manager mints TLS
   ─► agent replies: https://<name>.tools-dev.qualifiedhealthai.com  (reachable on Tailscale)

tool-reaper CronJob (daily 03:17 UTC) ─► deletes expired tools from git ─► Fleet prunes them
```

- **Agent** = Claude, called **direct via the Anthropic API** (no litellm/gateway), running as an
  in-cluster Deployment. Talks to Slack over Socket Mode, so nothing is exposed publicly.
- **Ingress** = internal only (class `nginx` → `10.0.0.8`); tools are reachable on the tailnet.
- **DB** = optional bundled Postgres per tool (StatefulSet + PVC in the tool's namespace),
  **non-PHI only**, dies with the tool.
- **TTL** = every tool gets a `qh-tool/expires-at` annotation; the reaper deletes expired tools
  by removing the folder **and** its kustomization line (Fleet then prunes the namespace).

## The app contract (what a tool's image must satisfy)

1. One container image in the qh-mgmt AR, built for **linux/amd64** (GKE nodes are amd64; an
   arm64-only image from an M-series Mac → `ErrImagePull` — use
   `docker buildx --platform linux/amd64` or `crane cp --platform linux/amd64`).
2. Listens on one HTTP port, bound to `0.0.0.0`.
3. Config from env vars only (no baked-in hostnames).
4. Stateless, or all state in the injected `DATABASE_URL` (`db: true`).
5. No PHI.

## Layout

```
agent/                          hosted Slack agent (the "just talk" front door)
  app.py                          Socket Mode bot → Claude → scaffold.py
  Dockerfile, entrypoint.sh       image (bakes in scaffold.py + template/)
  requirements.txt
  deploy/deployment.yaml          in-cluster Deployment
  deploy/README.md                one-time setup (Slack app, secrets, apply)
scaffold.py                     spec → rendered overlay → commit + push (the engine)
template/                       golden template (*.tmpl)
reaper/reaper.py, cronjob.yaml  TTL cleanup (scaffold.py in reverse)
.claude/skills/deploy-tool/     same procedure as a Claude Code skill (CLI fallback)
spec.example.yaml, spec.minimal.yaml   example specs
```

## Using it

Just DM the bot in Slack (or `@`-mention it):

> deploy hellotest, image `us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/hello:v1`, port 80, no db, 1 day

It confirms the spec, deploys, and replies with the URL.

## Operating it

Everything is one-time except rebuilding the agent image when you change agent code:

```bash
# rebuild + roll the hosted agent (must be amd64)
docker buildx build --platform linux/amd64 -f agent/Dockerfile \
  -t us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/deploy-agent:latest --push .
kubectl --context dev-control -n deploy-portal rollout restart deploy/deploy-agent
```

Full first-time install (Slack app, secrets, apply) is in [`agent/deploy/README.md`](agent/deploy/README.md).

## CLI fallback (no Slack)

The same engine runs standalone:

```bash
export QH_DEPLOYMENT_PATH=~/Desktop/REPOS/qh-deployment
pip install -r requirements.txt
python scaffold.py --spec spec.minimal.yaml --push
```
