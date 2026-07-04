#!/bin/sh
set -e
# Set up the write deploy key for qh-deployment (mounted at /keys/id).
mkdir -p "$HOME/.ssh"
cp /keys/id "$HOME/.ssh/id_ed25519"
chmod 600 "$HOME/.ssh/id_ed25519"
ssh-keyscan github.com >> "$HOME/.ssh/known_hosts" 2>/dev/null
export GIT_SSH_COMMAND="ssh -i $HOME/.ssh/id_ed25519 -o StrictHostKeyChecking=no"

: "${QH_DEPLOYMENT_PATH:=/work/qh-deployment}"
: "${REPO_URL:=ssh://git@github.com/Qualified-Health/qh-deployment}"
if [ ! -d "$QH_DEPLOYMENT_PATH/.git" ]; then
  git clone "$REPO_URL" "$QH_DEPLOYMENT_PATH"
fi
git -C "$QH_DEPLOYMENT_PATH" config user.email "deploybot@qualifiedhealthai.com"
git -C "$QH_DEPLOYMENT_PATH" config user.name "qh-deploy-portal-bot"

exec python /app/app.py
