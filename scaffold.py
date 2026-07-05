#!/usr/bin/env python3
"""scaffold.py — turn a tool spec into a deployed overlay in qh-deployment.

Component-loop-first: emits a Deployment + Service per component, marks exactly one
component public (it gets the ingress + URL), and optionally bundles a per-tool Postgres.
Writes qh-control/base/tools/<name>/ and appends the folder to base/kustomization.yaml,
then git commits (and optionally pushes). "Bring your own image" — no build here.

Usage:
    export QH_DEPLOYMENT_PATH=~/Desktop/REPOS/qh-deployment
    python scaffold.py --spec spec.example.yaml [--push]
"""
from __future__ import annotations

import argparse
import os
import re
import secrets
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template"
DOMAIN = "tools-dev.qualifiedhealthai.com"
NAME_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,38}[a-z0-9])?$")  # dns-safe, <=40


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def render(tmpl_name: str, vars: dict) -> str:
    text = (TEMPLATE / tmpl_name).read_text()
    for k, v in vars.items():
        text = text.replace("{{" + k + "}}", str(v))
    return text.rstrip("\n") + "\n"


def env_section(entries: list[dict], with_db: bool, tool: str) -> str:
    """Build the container `env:` block (10-space indent) or '' if empty."""
    lines: list[str] = []
    for e in entries or []:
        lines.append(f'            - name: {e["name"]}')
        lines.append(f'              value: "{e["value"]}"')
    if with_db:
        lines += [
            "            - name: DATABASE_URL",
            "              valueFrom:",
            "                secretKeyRef:",
            f"                  name: {tool}-db",
            "                  key: DATABASE_URL",
        ]
    if not lines:
        return ""
    return "          env:\n" + "\n".join(lines)


def validate(spec: dict) -> None:
    name = spec.get("name", "")
    if not NAME_RE.match(name):
        die(f"name '{name}' is not dns-safe (lowercase [a-z0-9-], <=40 chars)")
    comps = spec.get("components") or []
    if not comps:
        die("spec needs at least one component")
    public = [c for c in comps if c.get("public")]
    if len(public) != 1:
        die(f"exactly one component must be public: true (found {len(public)})")
    seen = set()
    for c in comps:
        for f in ("name", "image", "port"):
            if f not in c:
                die(f"component {c.get('name', '?')} missing '{f}'")
        if not NAME_RE.match(c["name"]):
            die(f"component name '{c['name']}' is not dns-safe")
        if c["name"] in seen:
            die(f"duplicate component name '{c['name']}'")
        seen.add(c["name"])


def update_base_kustomization(base_kust: Path, entry: str) -> None:
    text = base_kust.read_text()
    if f"- {entry}" in text:
        return
    lines = text.splitlines()
    out, inserted = [], False
    for line in lines:
        out.append(line)
        if not inserted and re.match(r"^resources:\s*$", line):
            out.append(f"  - {entry}")
            inserted = True
    if not inserted:
        out += ["resources:", f"  - {entry}"]
    base_kust.write_text("\n".join(out) + "\n")


def git(deployment_path: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(deployment_path), *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout.strip()


def commit_push(dep: Path, msg: str, push: bool) -> None:
    git(dep, "add", "-A")
    git(dep, "commit", "-m", msg)
    if push:
        git(dep, "pull", "--rebase")
        git(dep, "push", "origin", "main")


IMAGE_RE = re.compile(r"^(\s*image:\s*).*$", re.M)


def do_update(dep: Path, base: Path, name: str, image: str,
              component: str | None, push: bool) -> None:
    """Bump an existing tool's image tag. Same folder/namespace/ingress/URL/DB — just a new image."""
    if not NAME_RE.match(name):
        die(f"name '{name}' is not dns-safe")
    tool_dir = base / "tools" / name
    if not tool_dir.exists():
        die(f"tool '{name}' does not exist — deploy it first")
    if component:
        dep_file = tool_dir / f"{component}-deployment.yaml"
        if not dep_file.exists():
            die(f"component '{component}' not found for tool '{name}'")
    else:
        dep_files = sorted(tool_dir.glob("*-deployment.yaml"))
        if len(dep_files) != 1:
            die(f"tool '{name}' has {len(dep_files)} components; pass --component")
        dep_file = dep_files[0]
    text = dep_file.read_text()
    new_text, n = IMAGE_RE.subn(lambda m: m.group(1) + image, text)
    if n != 1:
        die(f"expected one image: line in {dep_file.name}, found {n}")
    dep_file.write_text(new_text)
    commit_push(dep, f"update tool {name}: image -> {image}", push)
    print(f"updated tool '{name}' ({dep_file.name}) -> {image}")
    print(f"URL (unchanged): https://{name}.{DOMAIN}")
    if not push:
        print("committed locally; re-run with --push to deploy")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spec", help="tool spec yaml (deploy mode)")
    ap.add_argument("--update", action="store_true", help="update an existing tool's image")
    ap.add_argument("--name", help="tool name (update mode)")
    ap.add_argument("--image", help="new image ref (update mode)")
    ap.add_argument("--component", help="component to update (default: the only one)")
    ap.add_argument("--push", action="store_true", help="also push to origin/main")
    ap.add_argument("--deployment-path", default=os.environ.get("QH_DEPLOYMENT_PATH"))
    ap.add_argument("--owner", default=os.environ.get("USER", "unknown"))
    args = ap.parse_args()

    if not args.deployment_path:
        die("set QH_DEPLOYMENT_PATH or pass --deployment-path (a qh-deployment clone)")
    dep = Path(args.deployment_path).expanduser()
    base = dep / "qh-control" / "base"
    if not (base / "kustomization.yaml").exists():
        die(f"{base}/kustomization.yaml not found — is this a qh-deployment clone?")

    if args.update:
        if not (args.name and args.image):
            die("update mode needs --name and --image")
        do_update(dep, base, args.name, args.image, args.component, args.push)
        return

    if not args.spec:
        die("deploy mode needs --spec (or use --update)")
    spec = yaml.safe_load(Path(args.spec).read_text())
    validate(spec)

    name = spec["name"]
    ttl_days = int(spec.get("ttl_days", 30))
    with_db = bool(spec.get("db", False))
    namespace = f"tools-{name}"
    host = f"{name}.{DOMAIN}"
    expires_at = (datetime.now(timezone.utc) + timedelta(days=ttl_days)).isoformat()
    safe = name.replace("-", "_")

    tool_dir = base / "tools" / name
    if tool_dir.exists():
        die(f"tool '{name}' already exists at {tool_dir} — pick another name")
    tool_dir.mkdir(parents=True)

    resources: list[str] = ["namespace.yaml"]

    (tool_dir / "namespace.yaml").write_text(render("namespace.yaml.tmpl", {
        "NAMESPACE": namespace, "NAME": name, "OWNER": args.owner,
        "EXPIRES_AT": expires_at,
    }))

    if with_db:
        (tool_dir / "postgres.yaml").write_text(render("postgres.yaml.tmpl", {
            "NAME": name, "NAMESPACE": namespace,
            "DB_NAME": f"{safe}_db", "DB_USER": f"{safe}_user",
            "DB_PASSWORD": secrets.token_urlsafe(18),
        }))
        resources.append("postgres.yaml")

    public_svc = public_port = None
    for c in spec["components"]:
        svc = f"{name}-{c['name']}"
        common = {
            "SVC_NAME": svc, "NAMESPACE": namespace, "NAME": name,
            "COMP_NAME": c["name"], "IMAGE": c["image"], "PORT": c["port"],
        }
        dep_yaml = render("deployment.yaml.tmpl", {
            **common, "ENV_SECTION": env_section(c.get("env"), with_db, name),
        })
        (tool_dir / f"{c['name']}-deployment.yaml").write_text(dep_yaml)
        (tool_dir / f"{c['name']}-service.yaml").write_text(
            render("service.yaml.tmpl", common))
        resources += [f"{c['name']}-deployment.yaml", f"{c['name']}-service.yaml"]
        if c.get("public"):
            public_svc, public_port = svc, c["port"]

    (tool_dir / "ingress.yaml").write_text(render("ingress.yaml.tmpl", {
        "NAME": name, "NAMESPACE": namespace, "HOST": host,
        "PUBLIC_SVC": public_svc, "PUBLIC_PORT": public_port,
    }))
    resources.append("ingress.yaml")

    (tool_dir / "kustomization.yaml").write_text(render("kustomization.yaml.tmpl", {
        "NAME": name,
        "RESOURCE_LIST": "\n".join(f"  - {r}" for r in resources),
    }))

    update_base_kustomization(base / "kustomization.yaml", f"tools/{name}")

    commit_push(dep, f"deploy tool {name} (ttl {ttl_days}d)", args.push)

    print(f"scaffolded tool '{name}' -> {tool_dir}")
    print(f"URL (after Fleet syncs ~30s + cert issues): https://{host}")
    if not args.push:
        print("committed locally; re-run with --push to deploy")


if __name__ == "__main__":
    main()
