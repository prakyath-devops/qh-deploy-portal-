#!/usr/bin/env python3
"""Slack deploy agent (Socket Mode).

A person @-mentions or DMs the bot in Slack. The agent (Claude, via the Anthropic API) can:
  - deploy a tool (bring-your-own-image) -> URL
  - update a tool's image (same URL/namespace/DB) after they push a fixed build
  - show status / logs of a tool so they can debug it themselves
It runs scaffold.py for the git changes; Fleet deploys. Nobody's laptop is involved.

Lifecycle model: the platform deploys + observes; it does NOT edit the tool's source. If the
logs show a CODE bug, the person fixes it in their repo, rebuilds the image, and asks the bot to
update — same URL. Operational issues (crash, bad env, DB down, ImagePull) the bot surfaces here.

Env:
  SLACK_BOT_TOKEN, SLACK_APP_TOKEN   Slack app credentials
  ANTHROPIC_API_KEY                  model access (direct, no litellm)
  ANTHROPIC_MODEL                    default claude-sonnet-5
  QH_DEPLOYMENT_PATH                 a writable clone of qh-deployment (default /work/qh-deployment)
  SCAFFOLD                           path to scaffold.py (default /app/scaffold.py)
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time

import yaml
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
DEPLOYMENT_PATH = os.environ.get("QH_DEPLOYMENT_PATH", "/work/qh-deployment")
SCAFFOLD = os.environ.get("SCAFFOLD", "/app/scaffold.py")

# Kubernetes client (read-only). In-cluster when hosted; kubeconfig when run locally.
K8S_OK = False
try:
    from kubernetes import client as k8s, config as k8s_config
    try:
        k8s_config.load_incluster_config()
        K8S_OK = True
    except Exception:
        k8s_config.load_kube_config()
        K8S_OK = True
except Exception as e:  # pragma: no cover
    logging.warning("kubernetes client unavailable: %s", e)

anthropic = Anthropic()
app = App(token=os.environ["SLACK_BOT_TOKEN"])
sessions: dict[str, list] = {}  # thread_ts -> message history (ephemeral)


def ns_of(name: str) -> str:
    return f"tools-{name}"


def pod_problems(ns: str) -> str:
    """Human-readable reasons any pod in the namespace is unhealthy (ImagePullBackOff, crash, ...)."""
    core = k8s.CoreV1Api()
    lines: list[str] = []
    try:
        pods = core.list_namespaced_pod(ns).items
    except Exception:
        return ""
    for p in pods:
        for cs in (p.status.container_statuses or []):
            w = getattr(cs.state, "waiting", None)
            if w and w.reason and w.reason != "ContainerCreating":
                lines.append(f"{p.metadata.name}/{cs.name}: {w.reason} — {(w.message or '').strip()[:200]}")
            t = getattr(cs.state, "terminated", None)
            if t and t.reason and t.reason != "Completed":
                lines.append(f"{p.metadata.name}/{cs.name}: {t.reason} (exit {t.exit_code})")
    return "\n".join(lines)


def verify_rollout(name: str, timeout: int = 75) -> tuple[bool, str]:
    """Poll the tool's Deployments until ready or timeout. Returns (ok, detail)."""
    if not K8S_OK:
        return True, ""  # can't verify — stay optimistic
    ns = ns_of(name)
    apps = k8s.AppsV1Api()
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            deps = apps.list_namespaced_deployment(ns).items
        except Exception:
            time.sleep(4)
            continue
        if deps and all((d.status.ready_replicas or 0) >= (d.spec.replicas or 1) for d in deps):
            return True, ""
        problems = pod_problems(ns)
        if problems:  # a hard failure — no point waiting the full timeout
            return False, problems
        time.sleep(5)
    return False, pod_problems(ns) or "not ready within timeout"


def tool_status(name: str) -> str:
    if not K8S_OK:
        return "cluster access not available"
    ns = ns_of(name)
    try:
        deps = k8s.AppsV1Api().list_namespaced_deployment(ns).items
    except Exception:
        return f"tool '{name}' not found (namespace {ns} missing)"
    if not deps:
        return f"tool '{name}': no deployments in {ns}"
    out = [f"{d.metadata.name}: {(d.status.ready_replicas or 0)}/{d.spec.replicas or 1} ready"
           for d in deps]
    probs = pod_problems(ns)
    if probs:
        out.append("issues:\n" + probs)
    return "\n".join(out)


def tool_logs(name: str, tail: int = 50) -> str:
    if not K8S_OK:
        return "cluster access not available"
    ns = ns_of(name)
    core = k8s.CoreV1Api()
    try:
        pods = core.list_namespaced_pod(ns).items
    except Exception:
        return f"tool '{name}' not found"
    if not pods:
        return f"tool '{name}': no pods"
    out: list[str] = []
    for p in pods:
        for c in (p.spec.containers or []):
            try:
                log = core.read_namespaced_pod_log(
                    p.metadata.name, ns, container=c.name, tail_lines=tail)
            except Exception as e:
                log = f"(no logs: {e.__class__.__name__})"
            out.append(f"[{p.metadata.name}/{c.name}]\n{(log or '').strip()[-1200:]}")
    return "\n\n".join(out)[:3500]


SYSTEM = """You are the QH internal-tool deploy agent, talking to a person in Slack.

You manage the DEPLOYMENT lifecycle of internal tools on qh-dev-control (URL:
https://<name>.tools-dev.qualifiedhealthai.com, on Tailscale). You do NOT edit tool source code.

Capabilities (tools): deploy_tool (new tool), update_tool (new image, same URL/DB), db_console
(web SQL console for a db:true tool — read-write, so people can query AND edit data with no new
image), tool_status, tool_logs. Use status/logs to help people debug. If logs show a CODE bug,
tell them: fix it in your repo, rebuild + push a new image, then I'll update_tool to that image
(same URL). Operational issues (ImagePullBackOff, crash, bad env) — explain what status/logs show.
For "query/see/edit the data" of a tool, offer db_console and return its URL.

Image contract: one container image in us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/<repo>:<tag>,
built linux/amd64, listens on one HTTP port on 0.0.0.0, config from env, stateless or uses the
injected DATABASE_URL (db:true). NO PHI.

Deploy rules:
- name: the tool/URL name ONLY — lowercase [a-z0-9-], <=40. NOT the image path.
- image: use the FULL image reference the person gives, verbatim. NEVER build it from the tool
  name. If missing, ASK for it.
- component name: "app" for a single-component tool; distinct names only for real multi-component.
- exactly one component public:true. ttl_days: ask if not given; suggest 7.

Procedure: gather fields, CONFIRM the spec in one short sentence, and only after they agree call
the tool. Report the URL/result or the exact error. Be concise — this is Slack."""

TOOLS = [
    {
        "name": "deploy_tool",
        "description": "Scaffold + deploy a NEW tool. Call ONLY after the person confirms the spec.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string",
                         "description": "Tool/URL name ONLY (lowercase [a-z0-9-], <=40). Never used as the image path."},
                "ttl_days": {"type": "integer"},
                "db": {"type": "boolean"},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string",
                                     "description": "Component name. Use 'app' for a single-component tool; never reuse the tool name."},
                            "image": {"type": "string",
                                      "description": "Full image reference verbatim from the user (registry/repo:tag). NEVER construct it from the tool name."},
                            "port": {"type": "integer"},
                            "public": {"type": "boolean"},
                            "env": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "value": {"type": "string"},
                                    },
                                    "required": ["name", "value"],
                                },
                            },
                        },
                        "required": ["name", "image", "port", "public"],
                    },
                },
            },
            "required": ["name", "ttl_days", "db", "components"],
        },
    },
    {
        "name": "update_tool",
        "description": "Redeploy an EXISTING tool with a new image (same URL, namespace, DB). Use after the person pushed a fixed/new build.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Existing tool name."},
                "image": {"type": "string", "description": "New full image reference (registry/repo:tag)."},
                "component": {"type": "string", "description": "Component to update; omit for single-component tools."},
            },
            "required": ["name", "image"],
        },
    },
    {
        "name": "db_console",
        "description": "Add a web SQL console (pgweb, READ-WRITE) for an existing db:true tool. "
                       "Returns a Tailscale URL to browse and edit the tool's Postgres directly "
                       "(no new image needed). NON-PHI tools only.",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "tool_status",
        "description": "Show rollout/pod status of a tool (and any ImagePull/crash reasons).",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
    {
        "name": "tool_logs",
        "description": "Show recent logs of a tool's pods (for debugging).",
        "input_schema": {"type": "object",
                         "properties": {"name": {"type": "string"},
                                        "tail": {"type": "integer", "description": "lines, default 50"}},
                         "required": ["name"]},
    },
]


def run_deploy(spec: dict) -> str:
    """Pull latest, write the spec, run scaffold.py --push, then verify the rollout."""
    try:
        subprocess.run(["git", "-C", DEPLOYMENT_PATH, "pull", "--rebase"],
                       check=True, capture_output=True, text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(spec, f)
            spec_path = f.name
        r = subprocess.run(
            ["python3", SCAFFOLD, "--spec", spec_path, "--push",
             "--deployment-path", DEPLOYMENT_PATH],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return f"Deploy failed:\n{r.stderr.strip() or r.stdout.strip()}"
        ok, detail = verify_rollout(spec["name"])
        base = r.stdout.strip()
        if ok:
            return base + "\n\n✅ Live and ready."
        return base + f"\n\n⚠️ Committed, but not healthy yet:\n{detail}\n" \
                      "Fix the image/config and use update, or check logs."
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.strip() if e.stderr else e}"


def run_update(name: str, image: str, component: str | None = None) -> str:
    try:
        subprocess.run(["git", "-C", DEPLOYMENT_PATH, "pull", "--rebase"],
                       check=True, capture_output=True, text=True)
        cmd = ["python3", SCAFFOLD, "--update", "--name", name, "--image", image,
               "--push", "--deployment-path", DEPLOYMENT_PATH]
        if component:
            cmd += ["--component", component]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            return f"Update failed:\n{r.stderr.strip() or r.stdout.strip()}"
        ok, detail = verify_rollout(name)
        base = r.stdout.strip()
        return base + ("\n\n✅ Rolled out and ready." if ok
                       else f"\n\n⚠️ Not healthy yet:\n{detail}")
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.strip() if e.stderr else e}"


def run_db_console(name: str) -> str:
    try:
        subprocess.run(["git", "-C", DEPLOYMENT_PATH, "pull", "--rebase"],
                       check=True, capture_output=True, text=True)
        r = subprocess.run(
            ["python3", SCAFFOLD, "--db-console", "--name", name, "--push",
             "--deployment-path", DEPLOYMENT_PATH],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return f"Couldn't create DB console:\n{r.stderr.strip() or r.stdout.strip()}"
        ok, detail = verify_rollout(name)
        base = r.stdout.strip()
        return base + ("\n\n✅ Console ready — read-write SQL over Tailscale (non-PHI)."
                       if ok else f"\n\n⚠️ Not ready yet:\n{detail}")
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.strip() if e.stderr else e}"


def dispatch(tool: str, inp: dict) -> str:
    if tool == "deploy_tool":
        return run_deploy(inp)
    if tool == "update_tool":
        return run_update(inp["name"], inp["image"], inp.get("component"))
    if tool == "db_console":
        return run_db_console(inp["name"])
    if tool == "tool_status":
        return tool_status(inp["name"])
    if tool == "tool_logs":
        return tool_logs(inp["name"], inp.get("tail", 50))
    return f"unknown tool {tool}"


def agent_turn(thread: str, user_text: str) -> str:
    history = sessions.setdefault(thread, [])
    history.append({"role": "user", "content": user_text})

    for _ in range(8):  # cap tool-use iterations
        resp = anthropic.messages.create(
            model=MODEL, max_tokens=1024, system=SYSTEM, tools=TOOLS, messages=history,
        )
        history.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            return "".join(b.text for b in resp.content if b.type == "text").strip()

        results = []
        for block in resp.content:
            if block.type == "tool_use":
                logging.info("tool_use %s %s", block.name, json.dumps(block.input)[:200])
                out = dispatch(block.name, block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        history.append({"role": "user", "content": results})

    return "I got stuck in a loop — please rephrase the request."


HELP = """👋 *I deploy internal tools to dev-control.* DM me and I hand you a live URL on Tailscale — no Kubernetes, no kubectl.

*First, push an image* (must be `linux/amd64`) to `us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/`. I deploy images — I don't build them.

*Then just tell me what to deploy:*
`deploy my-app, image us-central1-docker.pkg.dev/qh-mgmt-439315/qh-docker/my-app:v1 on port 8080, no db, 7 days`

I'll confirm, deploy, and reply with your URL.

*I also handle the whole lifecycle — just ask:*
• `status of my-app` — is it up?
• `logs for my-app` — recent logs
• `update my-app to …:v2` — new version, same URL, DB kept
• `open a db console for my-app` — web SQL console to view/edit data

*Two images?*
`deploy my-app, frontend …-fe:v1 on 3000, backend …-be:v1 on 8080, backend needs a db, 7 days`

⚠️ Internal, non-PHI, temporary tools only. URLs work on Tailscale/VPN. Tools auto-delete on their TTL.

📖 Full guide: https://app.notion.com/p/Deploy-Bot-Ship-Internal-Tools-from-Slack-3941358f13e180c19f87c25c8393a0c0"""


def is_help(text: str) -> bool:
    return text.strip().lower().lstrip("/") in ("help", "?", "commands")


@app.event("app_mention")
def on_mention(event, say):
    thread = event.get("thread_ts", event["ts"])
    text = event["text"].split(">", 1)[-1].strip()  # strip the @mention
    print(f"[mention] user={event.get('user')} text={text!r}")
    if is_help(text):
        say(text=HELP, thread_ts=thread)
        return
    reply = agent_turn(thread, text)
    print(f"[reply] {reply[:120]}")
    say(text=reply, thread_ts=thread)


@app.event("message")
def on_dm(event, say):
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        thread = event.get("thread_ts", event["ts"])
        print(f"[dm] user={event.get('user')} text={event['text']!r}")
        if is_help(event["text"]):
            say(text=HELP, thread_ts=thread)
            return
        reply = agent_turn(thread, event["text"])
        print(f"[reply] {reply[:120]}")
        say(text=reply, thread_ts=thread)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
