# Hosted deploy agent — one-time setup

After this runs, **nobody is ever involved**: a person talks to the bot in Slack, tools deploy,
the reaper cleans them up. You maintain only two secrets (rotate ~yearly) and the cluster.

## 1. Create the Slack app (once)

- api.slack.com → Create App → **enable Socket Mode** (gives an `xapp-` App token).
- Scopes (Bot Token → `xoxb-`): `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`.
- Event subscriptions: `app_mention`, `message.im`.
- Install to the workspace. Note the **Bot token** and **App token**.

## 2. Build + push the agent image (once; rebuild only to change the agent code)

```bash
# from the repo root
docker build -f agent/Dockerfile \
  -t us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/deploy-agent:latest .
docker push us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/deploy-agent:latest
```

## 3. Create the secrets on dev-control (once)

```bash
kubectl --context dev-control create namespace deploy-portal --dry-run=client -o yaml | kubectl apply -f -

kubectl --context dev-control -n deploy-portal create secret generic deploy-agent-secrets \
  --from-literal=SLACK_BOT_TOKEN=xoxb-... \
  --from-literal=SLACK_APP_TOKEN=xapp-... \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-...

# WRITE deploy key for qh-deployment (generate a keypair, add the PUBLIC key as a
# write deploy key on the qh-deployment repo, then load the PRIVATE key here):
kubectl --context dev-control -n deploy-portal create secret generic qh-deploy-key \
  --from-file=id=/path/to/deploy_key_private
```

## 4. Deploy the agent (+ reaper)

```bash
kubectl --context dev-control apply -f agent/deploy/deployment.yaml
# reaper: create the configmap from the real script, then apply the cronjob
kubectl --context dev-control -n deploy-portal create configmap reaper-src \
  --from-file=reaper.py=reaper/reaper.py --dry-run=client -o yaml | kubectl apply -f -
kubectl --context dev-control apply -f reaper/cronjob.yaml
```

## 5. Also required (platform-level, once)

- Wildcard DNS `*.tools-dev.qualifiedhealthai.com → 10.0.0.8` (internal ingress).
- The `qh-deploy-key` public half added as a **write** deploy key on `qh-deployment`.

Done. In Slack: `@deploybot deploy hello:1 on 8080, keep 7 days` → it replies with the URL.
