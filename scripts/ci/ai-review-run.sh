#!/usr/bin/env bash
set -eu

# The GitLab Docker runner is the outer isolation boundary. Its kernel blocks
# the user namespaces required by Codex's nested bubblewrap sandbox.
codex exec \
  --model "$OPENAI_MODEL" \
  --config "openai_base_url=${OPENAI_PROXY}" \
  --config 'model_reasoning_effort="medium"' \
  --sandbox danger-full-access \
  --ephemeral \
  --ignore-user-config \
  --color never \
  --output-last-message codex-review.md \
  review \
  --base "origin/${CI_MERGE_REQUEST_TARGET_BRANCH_NAME}" \
  --title "${CI_MERGE_REQUEST_TITLE}"
test -s codex-review.md

MR_API="${CI_API_V4_URL}/projects/${CI_PROJECT_ID}/merge_requests/${CI_MERGE_REQUEST_IID}"
REVIEW_MARKER='<!-- codex-ai-review -->'

# Do not let an obsolete pipeline overwrite the review for a newer MR head.
curl --fail-with-body --silent --show-error \
  --header "PRIVATE-TOKEN: ${GITLAB_CI_TOKEN}" \
  --output "$CODEX_HOME/merge-request.json" \
  "$MR_API"
CURRENT_HEAD_SHA="$(jq -r '.sha // .diff_refs.head_sha' "$CODEX_HOME/merge-request.json")"
if [ "$CURRENT_HEAD_SHA" != "$CI_COMMIT_SHA" ]; then
  echo "Skipping MR comment: reviewed ${CI_COMMIT_SHA}, current head is ${CURRENT_HEAD_SHA}."
  exit 0
fi

{
  printf '%s\n\n' "$REVIEW_MARKER"
  printf '## AI review by `%s`\n\n' "$OPENAI_MODEL"
  printf '_Reviewed commit [`%.12s`](%s/-/commit/%s) in [pipeline %s](%s)._\n\n' \
    "$CI_COMMIT_SHA" "$CI_PROJECT_URL" "$CI_COMMIT_SHA" "$CI_PIPELINE_ID" "$CI_PIPELINE_URL"
  cat codex-review.md
} > codex-review-note.md

# Restrict marker matching to notes owned by the API token's user.
curl --fail-with-body --silent --show-error \
  --header "PRIVATE-TOKEN: ${GITLAB_CI_TOKEN}" \
  --output "$CODEX_HOME/api-user.json" \
  "${CI_API_V4_URL}/user"
REVIEW_AUTHOR_ID="$(jq -r '.id' "$CODEX_HOME/api-user.json")"

NOTE_ID=""
PAGE=1
while [ -z "$NOTE_ID" ]; do
  curl --fail-with-body --silent --show-error \
    --header "PRIVATE-TOKEN: ${GITLAB_CI_TOKEN}" \
    --output "$CODEX_HOME/notes.json" \
    "${MR_API}/notes?per_page=100&page=${PAGE}&order_by=updated_at&sort=desc"
  NOTE_ID="$(jq -r \
    --arg marker "$REVIEW_MARKER" \
    --argjson author_id "$REVIEW_AUTHOR_ID" \
    '[.[] | select(.author.id == $author_id and (.body | contains($marker)))][0].id // empty' \
    "$CODEX_HOME/notes.json")"
  NOTE_COUNT="$(jq 'length' "$CODEX_HOME/notes.json")"
  if [ -n "$NOTE_ID" ] || [ "$NOTE_COUNT" -lt 100 ]; then
    break
  fi
  PAGE=$((PAGE + 1))
done

if [ -n "$NOTE_ID" ]; then
  echo "Updating Codex review note ${NOTE_ID}."
  curl --fail-with-body --silent --show-error \
    --request PUT \
    --header "PRIVATE-TOKEN: ${GITLAB_CI_TOKEN}" \
    --data-urlencode "body@codex-review-note.md" \
    --output "$CODEX_HOME/note-response.json" \
    "${MR_API}/notes/${NOTE_ID}"
else
  echo "Creating the initial Codex review note."
  curl --fail-with-body --silent --show-error \
    --request POST \
    --header "PRIVATE-TOKEN: ${GITLAB_CI_TOKEN}" \
    --data-urlencode "body@codex-review-note.md" \
    --output "$CODEX_HOME/note-response.json" \
    "${MR_API}/notes"
fi
