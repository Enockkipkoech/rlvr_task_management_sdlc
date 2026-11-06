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

mkdir -p "${FULL_LOG_DIR:-./runs/full}"

# Training always uses the train split of all six tasks.
TRAIN_ARGS='{"split":"train","random_seed":42}'

echo "Starting full SDLC RLVR training"
echo "Model: $MODEL_NAME"
echo "Steps: ${FULL_TRAIN_STEPS:-150}"

uv run python -m tinker_cookbook.recipes.verifiers_rl.train \
    "vf_env_id=${VF_ENV_ID}" \
    "vf_env_args=${TRAIN_ARGS}" \
    "model_name=${MODEL_NAME}" \
    "lora_rank=${LORA_RANK:-32}" \
    "learning_rate=${LEARNING_RATE:-1e-5}" \
    "max_tokens=${MAX_TOKENS:-8192}" \
    "max_steps=${FULL_TRAIN_STEPS:-150}" \
    "log_path=${FULL_LOG_DIR:-./runs/full}"
