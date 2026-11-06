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

mkdir -p "${SMOKE_LOG_DIR:-./runs/smoke}"

# Smoke run: the two cheapest tasks (1 schema, 3 crud) on the train split.
SMOKE_ARGS='{"split":"train","task_ids":[1,3],"random_seed":42}'

echo "Starting smoke training"
echo "Model: $MODEL_NAME"
echo "Steps: ${SMOKE_TRAIN_STEPS:-5}"

uv run python -m tinker_cookbook.recipes.verifiers_rl.train \
    "vf_env_id=${VF_ENV_ID}" \
    "vf_env_args=${SMOKE_ARGS}" \
    "model_name=${MODEL_NAME}" \
    "lora_rank=${LORA_RANK:-32}" \
    "learning_rate=${LEARNING_RATE:-1e-5}" \
    "max_tokens=${MAX_TOKENS:-8192}" \
    "max_steps=${SMOKE_TRAIN_STEPS:-5}" \
    "log_path=${SMOKE_LOG_DIR:-./runs/smoke}"
