---
name: deploy-tool
description: >
  Deploy an internal tool to qh-dev-control from a chat description. Use when someone wants
  to deploy/ship/host an app, container, or image as an internal tool and get a URL back.
  Turns a plain-English request into a tool spec, runs scaffold.py, and reports the URL.
---

# deploy-tool

You turn a person's description of an internal tool into a deployed app on **qh-dev-control**,
reachable over Tailscale at `https://<name>.tools-dev.qualifiedhealthai.com`.

You do NOT build images and you do NOT hand-write YAML. You produce a **spec**, run
`scaffold.py`, and report the result. Determinism lives in the template + scaffolder.

## The app contract (tell the person this if their image doesn't fit)

Their image must:
1. Be **one container image** already pushed to `us-docker.pkg.dev/qh-mgmt-439315/qh-docker/<name>:<tag>`.
2. **Listen on one HTTP port**, bound to `0.0.0.0` (not localhost).
3. Get all config from **env vars** — never bake in hostnames.
4. Be **stateless**, or put all state in the injected `DATABASE_URL` (set `db: true`).
5. Hold **no PHI** — the bundled Postgres has no backup/encryption. Non-PHI tools only.

Most tools are **one component + optional DB**. Use multiple components only for a genuinely
separate frontend + backend.

## Procedure

1. **Extract a spec** from the request:
   ```yaml
   name: <dns-safe, lowercase, [a-z0-9-], <=40>
   ttl_days: <number>          # ask if not given; default 7
   db: <true|false>            # true only if the app needs a database
   components:
     - name: app
       image: <full AR image ref>
       port: <container port>
       public: true            # exactly one component is public -> gets the URL
   ```
2. **Confirm** the spec back to the person in plain words (name, image, port, db?, TTL) and
   wait for a yes. If a required field is missing (image, port), ask — don't guess.
3. **Run the scaffolder** (it validates the name, rejects collisions, writes the overlay,
   commits, and pushes):
   ```bash
   QH_DEPLOYMENT_PATH=<qh-deployment clone> python scaffold.py --spec <spec.yaml> --push
   ```
4. **Verify** (you are on Tailscale with kubeconfig):
   ```bash
   kubectl --context dev-control -n tools-<name> rollout status deploy/<name>-<comp> --timeout=120s
   kubectl --context dev-control -n tools-<name> get ingress <name>
   ```
   Give Fleet ~30s to sync first. cert-manager issues TLS on first request to the host.
5. **Report**: the URL, whether it has a DB, and the expiry date. If a pod is `ImagePullBackOff`,
   the image isn't in the qh-mgmt registry or isn't pullable — say so. If `CrashLoopBackOff`,
   surface the pod logs.

## Notes
- Redeploy of an existing tool = bump the image tag in its folder (not a new scaffold). If the
  folder already exists, scaffold.py rejects it — edit the tag instead.
- Everything lands in `qh-deployment` under `qh-control/base/tools/<name>/`. `qh-platform` is
  never touched.
