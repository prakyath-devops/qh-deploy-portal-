# qh-deploy-portal

Agent-driven "bring your own image → get a URL" for internal tools on **qh-dev-control**.

This repo is **the machine** — the golden template, the scaffolder, the agent skill, and the
TTL reaper. It does **not** hold tool source or deployed manifests:

- Tool **source** lives in the tool author's own repo; they build + push an **image** to the
  qh-mgmt Artifact Registry (`us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/<tool>:<tag>`).
- Tool **manifests** (the generated overlays) are written into **`qh-deployment`** at
  `qh-control/base/tools/<name>/`, where Fleet already watches and deploys to dev-control.
- **`qh-platform` is never touched.**

## How it works (v1)

```
person (chat) ─► Claude Code + deploy-tool skill ─► scaffold.py
   ─► writes qh-control/base/tools/<name>/ in qh-deployment ─► commit+push
   ─► Fleet deploys to qh-dev-control ─► cert-manager TLS
   ─► URL: https://<name>.tools-dev.qualifiedhealthai.com  (reachable on Tailscale)
```

- **Agent** = Claude Code on your laptop (on Tailscale), model = **Anthropic API direct**
  (`ANTHROPIC_API_KEY`), no litellm, no gateway.
- **Ingress** = internal only (class `nginx` → `10.0.0.8`); tools are reachable on the tailnet.
- **DB** = optional bundled Postgres per tool (StatefulSet + PVC in the tool's namespace),
  **non-PHI only**, dies with the tool.
- **TTL** = every tool gets `qh-tool/expires-at`; the reaper CronJob deletes expired tools.

## Layout

```
.claude/skills/deploy-tool/SKILL.md   the agent procedure (auto-loads in Claude Code)
scaffold.py                           spec → rendered overlay → commit + push
template/                             golden template (*.tmpl)
reaper/                               TTL CronJob + reaper.py
spec.example.yaml                     example tool spec
requirements.txt                      pyyaml
```

## One-time setup

1. Wildcard DNS `*.tools-dev.qualifiedhealthai.com → 10.0.0.8`.
2. Write deploy key on `qh-deployment` (scaffolder + reaper push with it).
3. `export ANTHROPIC_API_KEY=...` for Claude Code.
4. `pip install -r requirements.txt`.

## Usage

```bash
# clone the GitOps repo somewhere the scaffolder can write
export QH_DEPLOYMENT_PATH=~/Desktop/REPOS/qh-deployment

python scaffold.py --spec spec.example.yaml          # render + write + commit (local)
python scaffold.py --spec spec.example.yaml --push    # also push to origin/main
```

Or just tell Claude Code: *"deploy hello:1 on port 8080, no db, keep 7 days"* — the skill runs
`scaffold.py` for you and reports the URL.
