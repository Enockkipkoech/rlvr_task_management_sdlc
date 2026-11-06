#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

set -a
source .env
set +a

: "${VF_ENV_ID:?Set VF_ENV_ID in .env}"
: "${MODEL_NAME:?Set MODEL_NAME in .env}"
: "${TEST_DATABASE_ADMIN_URL:?Set TEST_DATABASE_ADMIN_URL in .env}"

if [[ ! -d "${FULL_LOG_DIR:-./runs/full}" ]]; then
    echo "Training output directory not found."
    echo "Run scripts/04_full_train.sh first."
    exit 1
fi

mkdir -p "${POST_EVAL_LOG_DIR:-./runs/post_eval}"

echo "Running post-training evaluation (same validation tasks and graders as the baseline)"
echo "Base model: $MODEL_NAME"

# UNVERIFIED: how tinker_cookbook's evaluate recipe loads the trained checkpoint.
# MODEL_PATH is passed as `model_path=...` when set; confirm the argument name in the
# recipe (python -m tinker_cookbook.recipes.verifiers_rl.evaluate --help) before trusting
# these numbers, otherwise this re-evaluates the base model.
EXTRA=()
if [[ -n "${MODEL_PATH:-}" ]]; then
    EXTRA+=("model_path=${MODEL_PATH}")
else
    echo "WARNING: MODEL_PATH is not set; this run evaluates the BASE model."
fi

uv run python -m tinker_cookbook.recipes.verifiers_rl.evaluate \
    "vf_env_id=${VF_ENV_ID}" \
    "vf_env_args=${VF_ENV_ARGS:-{}}" \
    "model_name=${MODEL_NAME}" \
    "max_tokens=${MAX_TOKENS:-8192}" \
    "log_path=${POST_EVAL_LOG_DIR:-./runs/post_eval}" \
    ${EXTRA[@]+"${EXTRA[@]}"}
