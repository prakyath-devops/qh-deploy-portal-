#!/usr/bin/env python3
"""reaper.py — scaffold.py in reverse. Deletes EXPIRED tools from git.

Reads each tool's namespace.yaml annotation `qh-tool/expires-at`. If it's in the past,
removes the tool folder AND its line in base/kustomization.yaml, then commits + pushes.
Fleet (keepResources: false) then removes the namespace + PVC from the cluster.

It deletes GIT, never the namespace directly — deleting the namespace would let Fleet
recreate it from the still-present folder on the next sync.

Runs as an in-cluster CronJob (see cronjob.yaml). Env:
    REPO_URL   ssh url of qh-deployment (default: the qh-deployment repo)
    WORKDIR    where to clone (default /work/qh-deployment)
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_URL = os.environ.get("REPO_URL", "ssh://git@github.com/Qualified-Health/qh-deployment")
WORKDIR = Path(os.environ.get("WORKDIR", "/work/qh-deployment"))


def sh(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(args, cwd=str(cwd) if cwd else None, check=True)


def expired(ns_yaml: Path) -> bool:
    doc = yaml.safe_load(ns_yaml.read_text())
    exp = (doc.get("metadata", {}).get("annotations", {}) or {}).get("qh-tool/expires-at")
    if not exp:
        return False
    return datetime.now(timezone.utc) >= datetime.fromisoformat(exp)


def remove_kustomization_entry(base_kust: Path, name: str) -> None:
    lines = base_kust.read_text().splitlines()
    kept = [ln for ln in lines if ln.strip() != f"- tools/{name}"]
    base_kust.write_text("\n".join(kept) + "\n")


def main() -> None:
    if WORKDIR.exists():
        shutil.rmtree(WORKDIR)
    sh("git", "clone", "--depth", "1", REPO_URL, str(WORKDIR))
    sh("git", "-C", str(WORKDIR), "config", "user.email", "reaper@qualifiedhealthai.com")
    sh("git", "-C", str(WORKDIR), "config", "user.name", "qh-deploy-portal-reaper")

    tools_dir = WORKDIR / "qh-control" / "base" / "tools"
    base_kust = WORKDIR / "qh-control" / "base" / "kustomization.yaml"
    if not tools_dir.exists():
        print("no tools/ dir — nothing to reap")
        return

    reaped = []
    for tool in sorted(tools_dir.iterdir()):
        ns = tool / "namespace.yaml"
        if tool.is_dir() and ns.exists() and expired(ns):
            print(f"reaping expired tool: {tool.name}")
            shutil.rmtree(tool)
            remove_kustomization_entry(base_kust, tool.name)
            reaped.append(tool.name)

    if not reaped:
        print("nothing expired")
        return

    sh("git", "-C", str(WORKDIR), "add", "-A")
    sh("git", "-C", str(WORKDIR), "commit", "-m",
       f"reap expired tools: {', '.join(reaped)}")
    sh("git", "-C", str(WORKDIR), "push", "origin", "main")
    print(f"reaped {len(reaped)}: {reaped} — Fleet will remove the namespaces")


if __name__ == "__main__":
    main()
