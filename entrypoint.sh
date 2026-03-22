#!/bin/sh

# Load GitHub PAT if available
if [ -f /run/secrets/github-pat ]; then
    export GH_TOKEN=$(cat /run/secrets/github-pat)
fi

# Load Anthropic API key if available
if [ -f /run/secrets/anthropic-api-key ]; then
    export ANTHROPIC_API_KEY=$(cat /run/secrets/anthropic-api-key)
fi

exec nanobot "$@"
