#!/bin/sh

# Load GitHub PAT if available
if [ -f /run/secrets/github-pat ]; then
    export GH_TOKEN=$(cat /run/secrets/github-pat)
fi

# Set up claude user for Claude CLI one-shots
# Sync Claude auth from root mount to claude user's home
if [ -d /root/.claude ]; then
    cp -r /root/.claude/* /home/claude/.claude/ 2>/dev/null || true
    cp /root/.claude.json /home/claude/.claude.json 2>/dev/null || true
    chown -R claude:claude /home/claude/.claude /home/claude/.claude.json 2>/dev/null || true
fi

# Give claude user access to worktrees and hashi repo
chown -R claude:claude /root/hashi-worktrees 2>/dev/null || true
chmod -R a+rw /root/hashi 2>/dev/null || true

# Make GH_TOKEN available to claude user
echo "export GH_TOKEN=$GH_TOKEN" > /home/claude/.env 2>/dev/null || true
chown claude:claude /home/claude/.env 2>/dev/null || true

exec nanobot "$@"
