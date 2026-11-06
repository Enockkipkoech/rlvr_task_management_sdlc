# Task Management SDLC RLVR Case Study

Train and evaluate an LLM on six real backend-engineering tasks (database schema, auth, CRUD, authorization, validation/SQL safety, end-to-end delivery) for a FastAPI + PostgreSQL + JWT Task Management API. Rewards come from executing the model's code against an isolated PostgreSQL database, not from keyword matching or a judge model.

Built as a Verifiers v1 environment (`Task` / `Taskset` / `@vf.reward`), the same stack as `environments/pii_masking`. See `architecture.md` for diagrams.

## Layout

```text
task_management_rlvr/
├── README.md
├── architecture.md
├── pyproject.toml
├── .env.example
├── docker-compose.yml          local PostgreSQL for the evaluator (dev only)
├── src/task_management_rlvr/
│   ├── environment.py          Verifiers taskset, task, 4 weighted rewards
│   ├── tasks.py                six task specs, prompts, parameter space, gold reply
│   ├── dataset.py              deterministic rows, train/validation split, balancing
│   ├── validators.py           <implementation> artifact parsing and path rules
│   ├── evaluator.py            sandbox + temp DB + grader run + scoring
│   └── rewards.py              reward breakdown, weights, de-duplicating cache
├── tests/
│   ├── test_rewards.py         offline unit tests (no database)
│   ├── test_task_01_schema.py ... test_task_06_e2e.py   the six graders
│   ├── test_graders_discriminate.py   broken implementations must score below gold
│   ├── conftest.py, helpers.py
├── scripts/
│   ├── 00_check_env.py         setup check; --gold scores the gold solution (expects 1.0)
│   ├── 01_generate_dataset.py
│   ├── 02_baseline_eval.sh  03_smoke_train.sh  04_full_train.sh  05_post_eval.sh
│   └── score_reply.py          score one reply file
└── task_api_reference/         the gold solution (FastAPI app, SQL migration, Dockerfile, docs, tests)
```

## Quick start

```bash
cd environments/task_management_rlvr          # wherever you drop this folder
cp .env.example .env
docker compose up -d postgres                  # or point TEST_DATABASE_ADMIN_URL at any PostgreSQL 15+
uv sync
uv run python scripts/00_check_env.py --gold   # all six tasks must print reward=1.000
uv run pytest tests/test_rewards.py            # offline tests
uv run pytest                                  # everything, including graders and mutants (needs PostgreSQL)
```

Run the reference API on its own:

```bash
cd task_api_reference
cp .env.example .env     # set JWT_SECRET and POSTGRES_PASSWORD
docker compose up --build
```

## The tasks and rewards

Each task asks the model for a fixed set of files (table in `architecture.md`) in this format:

```text
<implementation>
<file path="app/security.py">
...complete file...
</file>
</implementation>
```

Files outside the task's owned set are ignored and forfeit the quality score. Reward per rollout:

| Category | Weight | Meaning |
| --- | --- | --- |
| functional | 0.50 | endpoint behaviour, status codes, persistence |
| database | 0.20 | constraints, FK rules, transactions, concurrency |
| security | 0.20 | hashing, JWT validation, authorization, SQL injection, leakage |
| quality | 0.10 | API contracts, error shape, static checks, docs, coverage |

Total is 1.0. Each category is passed/total over that task's tests for the category.

## Dataset

132 rows (108 train, 24 validation) from task x parameter combination x prompt framing x examples flag. Parameters (password minimum 8/10/12, title maximum 80/120/200, token TTL 15/30/60) change the prompt and the grader together. Train is balanced to equal rows per task (264 rows). Validation holds out every 5th row per task, so tasks 2 and 5 are evaluated on unseen parameter combinations; tasks 1, 3, 4, 6 have a single validation prompt each, so sample several rollouts per prompt when comparing models.

## Training with Tinker

The scripts mirror `pii_masking`: `02_baseline_eval.sh` (base model), `03_smoke_train.sh` (tasks 1 and 3, 5 steps), `04_full_train.sh`, `05_post_eval.sh`. Compare baseline and post-eval per task and per category to see which SDLC capabilities improved.

## Security: read before training

The evaluator executes code written by a model. It runs each rollout in a subprocess with a scrubbed environment (no API keys, no admin database URL), a role-scoped throwaway database, a timeout and resource limits. That is not isolation from the network or the host filesystem. For real training runs, execute the whole process inside a container or VM with no network egress and no credentials. Do not run it on a machine holding secrets.

## Throughput and cost

One evaluation takes about 1 to 15 seconds for tasks 1, 3 and 4, and 10 to 15 seconds for tasks 2, 5 and 6 on one core (Argon2 hashing dominates). A step with 4 groups of 8 rollouts is 32 evaluations; `TMR_MAX_PARALLEL_EVALS` bounds concurrency per process, so give the box enough cores and raise it accordingly. Four rewards share one run through the cache.

## Known limits

- The source-tree layout is required: the package locates `task_api_reference/` and `tests/` relative to the repo (override with `TMR_PROJECT_ROOT`). It does not work as an installed wheel alone.
- Task 6 coverage and "real test" checks are heuristics (a coverage floor and assertions per test). They can be gamed by tests that execute code without checking behaviour.
- Static code-quality checks (no string-built SQL, explicit transaction in delete) are heuristics; the behavioural tests are the real guard.
- Not verified here: the `tinker_cookbook` arguments for loading a trained checkpoint in `05_post_eval.sh`, and Tinker training itself (no Tinker access). Verified here: gold = 1.0 on all six tasks, 11 mutants score below gold, offline tests, and taskset load under Verifiers 0.4.1 on Python 3.12 with PostgreSQL 16.
