#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

set -a
source .env
set +a

: "${VF_ENV_ID:?Set VF_ENV_ID in .env}"
: "${MODEL_NAME:?Set MODEL_NAME in .env}"
: "${TEST_DATABASE_ADMIN_URL:?Set TEST_DATABASE_ADMIN_URL in .env (PostgreSQL for the sandboxed graders)}"

mkdir -p "${BASELINE_LOG_DIR:-./runs/baseline}"

echo "Running baseline evaluation (base model, identical tasks and graders as post-eval)"
echo "Environment: $VF_ENV_ID"
echo "Model:       $MODEL_NAME"

uv run python -m tinker_cookbook.recipes.verifiers_rl.evaluate \
    "vf_env_id=${VF_ENV_ID}" \
    "vf_env_args=${VF_ENV_ARGS:-{}}" \
    "model_name=${MODEL_NAME}" \
    "max_tokens=${MAX_TOKENS:-8192}" \
    "log_path=${BASELINE_LOG_DIR:-./runs/baseline}"
