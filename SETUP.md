# qh-deploy-portal — Setup Guide

Everything needed to stand up the platform from scratch on **qh-dev-control**. After this, people
deploy/update/observe/query internal tools by chatting with a Slack bot — hands-off.

All commands use `kubectl --context dev-control`. You must be **on Tailscale** to reach the cluster.

---

## 0. What already exists on dev-control (no action needed)

The platform reuses these — verified present:

| Capability | Detail |
|---|---|
| GitOps engine | Fleet `GitRepo` `qh-control-dev` watches `qh-deployment` path `qh-control`, target `clusterType=qh-dev-control` |
| Internal ingress | `ingress-nginx` = internal GCP LB at **`10.0.0.8`**, ingressClassName **`nginx`** |
| Auto TLS | cert-manager + `ClusterIssuer letsencrypt-dns01` (READY) |
| Storage | `standard-rwo` (default) for PVCs |
| Network | Tailscale subnet-router advertises the cluster subnet |
| Registry | Artifact Registry `us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker` (dev-control pulls natively) |

---

## 1. Prerequisites (on your machine)

- `kubectl` with a `dev-control` context, **on Tailscale**.
- `docker` + `buildx` (for the amd64 agent image).
- AR push auth: `gcloud auth configure-docker us-central1-docker.pkg.dev`.
- Write access to the `qh-deployment` GitHub repo (to add a deploy key).
- Slack workspace admin (to create the app).
- An **Anthropic API key**.

---

## 2. One-time platform setup

### 2a. Wildcard DNS (required — tools/consoles won't resolve or get TLS without it)
Create in NextDNS / the authoritative zone:
```
*.tools-dev.qualifiedhealthai.com   A   10.0.0.8
```
Covers every tool (`<name>.tools-dev…`) and DB console (`<name>-db.tools-dev…`).

### 2b. Write deploy key for qh-deployment
The bot + reaper push overlays to `qh-deployment` over SSH:
```bash
ssh-keygen -t ed25519 -f ~/.ssh/qh-deployment-deploy-key -N "" -C "qh-deploy-portal"
```
- Add the **public** half (`~/.ssh/qh-deployment-deploy-key.pub`) to
  GitHub → `qh-deployment` → Settings → Deploy keys → **Add**, with **Allow write access** checked.
- Keep the private half for step 3.

### 2c. Slack app (Socket Mode)
At api.slack.com/apps → Create App (from scratch):
- **Socket Mode**: enable → generate an **App-Level Token** (`xapp-…`), scope `connections:write`.
- **OAuth & Permissions → Bot Token Scopes**: `app_mentions:read`, `chat:write`, `im:history`,
  `im:read`, `im:write`.
- **Event Subscriptions**: enable → **Subscribe to bot events**: `app_mention`, `message.im`.
- **Install to Workspace** → copy the **Bot token** (`xoxb-…`).

### 2d. Anthropic key
Have an `ANTHROPIC_API_KEY` (`sk-ant-…`) ready.

---

## 3. Build the agent image (amd64 — required)

GKE nodes are amd64; an arm64 image (e.g. plain `docker build` on an M-series Mac) → `ImagePullBackOff`.
From the repo root:
```bash
docker buildx build --platform linux/amd64 -f agent/Dockerfile \
  -t us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/deploy-agent:latest --push .
```

---

## 4. Deploy on dev-control

```bash
# namespace
kubectl --context dev-control create ns deploy-portal --dry-run=client -o yaml | kubectl --context dev-control apply -f -

# secrets: Slack + Anthropic
kubectl --context dev-control -n deploy-portal create secret generic deploy-agent-secrets \
  --from-literal=SLACK_BOT_TOKEN=xoxb-... \
  --from-literal=SLACK_APP_TOKEN=xapp-... \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...

# secret: the write deploy key (private half from 2b)
kubectl --context dev-control -n deploy-portal create secret generic qh-deploy-key \
  --from-file=id=$HOME/.ssh/qh-deployment-deploy-key

# agent: read-only RBAC + Deployment
kubectl --context dev-control apply -f agent/deploy/rbac.yaml
kubectl --context dev-control apply -f agent/deploy/deployment.yaml

# reaper: ConfigMap from the REAL reaper.py, then the CronJob
kubectl --context dev-control -n deploy-portal create configmap reaper-src \
  --from-file=reaper.py=reaper/reaper.py --dry-run=client -o yaml | kubectl --context dev-control apply -f -
kubectl --context dev-control apply -f reaper/cronjob.yaml
```

---

## 5. Verify

```bash
kubectl --context dev-control -n deploy-portal rollout status deploy/deploy-agent --timeout=120s
kubectl --context dev-control -n deploy-portal logs deploy/deploy-agent --tail=5
# expect: cloned qh-deployment + "⚡️ Bolt app is running!"
```
Then DM the bot in Slack: `deploy hello, image us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/hello:v1, port 80, no db, 1 day`.

---

## 6. Using it (in Slack)

| Say | Result |
|---|---|
| `deploy <name>, image …, port …, [db], <n> days` | new tool → `https://<name>.tools-dev.qualifiedhealthai.com` |
| `update <name> [<component>] to <image>` | new image, **same URL/DB** |
| `open a db console for <name>` | read-write pgweb SQL UI at `<name>-db.tools-dev…` |
| `status of <name>` / `logs for <name>` | health + logs (self-debug) |

**Tool image contract:** one container, **linux/amd64**, in the qh-mgmt AR, listens on one HTTP port
on `0.0.0.0`, config from env, stateless or uses injected `DATABASE_URL`. **Non-PHI only.**

---

## 7. Operating

- **Change agent code** → rebuild (step 3) + `kubectl --context dev-control -n deploy-portal rollout restart deploy/deploy-agent`.
- **Iterate locally** → first pause the hosted bot so two bots don't split Slack events:
  `kubectl --context dev-control -n deploy-portal scale deploy/deploy-agent --replicas=0` (restore with `--replicas=1`).
- **TTL** → the reaper (daily 03:17 UTC) deletes expired tools; nothing else to do.

---

## 8. Gotchas (learned the hard way)

- **amd64 only** — arm64 images → `ImagePullBackOff`. Use `buildx --platform linux/amd64` or
  `crane cp --platform linux/amd64`.
- **Never run the local bot while the hosted one is up** — Socket Mode load-balances events across
  all connected clients; a stale local bot answers ~half the messages.
- **Registry is `us-central1-docker.pkg.dev`** (not `us-docker`).
- **Bundled Postgres has no SSL** — clients defaulting to `sslmode=require` (e.g. pgweb/lib/pq) must
  use `sslmode=disable`; the pgweb console template already does.
- **Deletion must remove the folder AND its `- tools/<name>` kustomization line** — folder-only
  breaks the whole qh-control bundle. The reaper does both; don't hand-delete just the folder.
- **DB tools may restart 1–2× on first boot** (start before Postgres is ready; self-heals).
- **Non-PHI only** — no CMEK/backup on bundled Postgres; consoles are read-write over Tailscale.
