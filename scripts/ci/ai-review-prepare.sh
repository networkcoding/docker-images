#!/usr/bin/env bash
set -eu

export OPENAI_API_KEY="${OPENAI_API_KEY:-}"
test -n "${OPENAI_MODEL:-}" || export OPENAI_MODEL="gpt-5.5"
test -n "$OPENAI_API_KEY" || { echo "OPENAI_API_KEY is not set" >&2; exit 1; }
test -n "${OPENAI_PROXY:-}" || { echo "OPENAI_PROXY is not set" >&2; exit 1; }
test -n "${GITLAB_CI_TOKEN:-}" || { echo "GITLAB_CI_TOKEN is not set" >&2; exit 1; }

mkdir -p "$CODEX_HOME/skills"
echo "+++++++++++++ OPENAI_MODEL: $OPENAI_MODEL +++++++++++++"
cp -R .agents/skills/cpp-merge-request-review "$CODEX_HOME/skills/"
printf '%s' "$OPENAI_API_KEY" | codex login --with-api-key

git fetch --no-tags origin \
  "+refs/heads/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}:refs/remotes/origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}"
