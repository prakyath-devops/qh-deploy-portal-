#!/usr/bin/env python3
"""Slack deploy agent (Socket Mode).

A person @-mentions or DMs the bot in Slack:  "deploy hello:1 on 8080, keep 7 days".
The agent (Claude, called directly via the Anthropic API) extracts a spec, confirms it,
then runs scaffold.py — which commits the overlay to qh-deployment; Fleet deploys it to
qh-dev-control. The agent replies with the URL. Nobody's laptop is involved.

Socket Mode = the bot dials out to Slack, so NOTHING is exposed publicly.

Env:
  SLACK_BOT_TOKEN, SLACK_APP_TOKEN   Slack app credentials
  ANTHROPIC_API_KEY                  model access (direct, no litellm)
  ANTHROPIC_MODEL                    default claude-sonnet-5
  QH_DEPLOYMENT_PATH                 a writable clone of qh-deployment (default /work/qh-deployment)
  SCAFFOLD                           path to scaffold.py (default /app/scaffold.py)
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile

import yaml
from anthropic import Anthropic
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
DEPLOYMENT_PATH = os.environ.get("QH_DEPLOYMENT_PATH", "/work/qh-deployment")
SCAFFOLD = os.environ.get("SCAFFOLD", "/app/scaffold.py")

anthropic = Anthropic()
app = App(token=os.environ["SLACK_BOT_TOKEN"])
sessions: dict[str, list] = {}  # thread_ts -> message history (ephemeral)

SYSTEM = """You are the QH internal-tool deploy agent, talking to a person in Slack.

You turn a plain-English request into a deployed internal tool on qh-dev-control, reachable at
https://<name>.tools-dev.qualifiedhealthai.com (on Tailscale). You do NOT build images or write
YAML — you produce a spec and call deploy_tool.

The person's image must: be one container image already pushed to
us-docker.pkg.dev/qh-mgmt-439315/qh-docker/<name>:<tag>; listen on one HTTP port bound to
0.0.0.0; read config from env; be stateless OR use the injected DATABASE_URL (db:true). NO PHI.

Most tools are ONE component + optional DB. Rules:
- name: lowercase [a-z0-9-], <=40 chars.
- exactly one component has public:true (it gets the URL).
- ttl_days: ask if not given; suggest 7.
Procedure: gather the fields, CONFIRM the spec back in one short sentence, and only after the
person agrees, call deploy_tool. Report the URL + TTL, or the exact error if it fails. Be concise
— this is Slack."""

TOOLS = [
    {
        "name": "deploy_tool",
        "description": "Scaffold + deploy a tool. Call ONLY after the person confirms the spec.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "ttl_days": {"type": "integer"},
                "db": {"type": "boolean"},
                "components": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "image": {"type": "string"},
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
]


def run_deploy(spec: dict) -> str:
    """Pull latest, write the spec, run scaffold.py --push. Return a human-readable result."""
    try:
        subprocess.run(["git", "-C", DEPLOYMENT_PATH, "pull", "--rebase"],
                       check=True, capture_output=True, text=True)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            yaml.safe_dump(spec, f)
            spec_path = f.name
        r = subprocess.run(
            ["python", SCAFFOLD, "--spec", spec_path, "--push",
             "--deployment-path", DEPLOYMENT_PATH],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return f"Deploy failed:\n{r.stderr.strip() or r.stdout.strip()}"
        return (r.stdout.strip() +
                "\n\nFleet is syncing (~30s); TLS is issued on first request. "
                "Give it a minute, then open the URL on Tailscale.")
    except subprocess.CalledProcessError as e:
        return f"git error: {e.stderr.strip() if e.stderr else e}"


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
            if block.type == "tool_use" and block.name == "deploy_tool":
                out = run_deploy(block.input)
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": out})
        history.append({"role": "user", "content": results})

    return "I got stuck in a loop — please rephrase the request."


@app.event("app_mention")
def on_mention(event, say):
    thread = event.get("thread_ts", event["ts"])
    text = event["text"].split(">", 1)[-1].strip()  # strip the @mention
    say(text=agent_turn(thread, text), thread_ts=thread)


@app.event("message")
def on_dm(event, say):
    if event.get("channel_type") == "im" and not event.get("bot_id"):
        thread = event.get("thread_ts", event["ts"])
        say(text=agent_turn(thread, event["text"]), thread_ts=thread)


if __name__ == "__main__":
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
