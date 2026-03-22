#!/bin/sh

# Load GitHub PAT if available
if [ -f /run/secrets/github-pat ]; then
    export GH_TOKEN=$(cat /run/secrets/github-pat)
fi

exec nanobot "$@"
