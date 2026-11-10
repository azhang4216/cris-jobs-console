# cris-jobs-console — training dashboard and infrastructure

**Researchers work only in [dmasif-experiments](https://github.com/azhang4216/dmasif-experiments#readme).** Create a `runs/<experiment>` branch from its `main`, edit research code or run settings, commit, and push. This repository runs the web console and submits the resulting training job; it is maintained by the operator.

The console trains the exact submitted commit on the existing **dataset_v2**, runs periodic held-out evaluation and a final evaluation of the best checkpoint, and displays status, step metrics, evaluations, checkpoints, and recent logs. Batch size is configurable in the research repository. Researchers do not select datasets, rebuild caches, edit Slurm files, or change console infrastructure.

**Release status:** the training integration is implemented locally. The November 10, 2026 VM inspection was read-only; this update has not been deployed or tested with a real GPU training submission. See [training deployment](docs/training-deployment.md) for the remaining operator activation and acceptance steps. Earlier extraction acceptance results are historical evidence, not training acceptance.

## Research workflow

The [researcher guide](docs/researcher.md) explains the complete flow:

1. Branch from `dmasif-experiments/main` into `runs/my-experiment`.
2. Edit `src/surface_encoder.py`, other research code, or `experiments/run.yaml`.
3. Push the commit; the signed GitHub webhook creates a run when submissions are enabled.
4. Open that run in the console to follow training and held-out evaluation.

Defaults reproduce the observed v8 preset: batch **32**, **10,000** training steps, evaluation and checkpoint saves every **125** steps. Training data is fixed; evaluation uses the separate `valset_v1v2` holdout. New runs always get separate output directories and do not auto-resume old checkpoints. The earlier trigger-test branches can stay untouched.

## Run the local demo

Requires Python 3.12+ and Git:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
dmasif-console demo --serve
```

Open **http://127.0.0.1:8000**. Two simulated training runs exercise the signed-push lifecycle, step/evaluation/checkpoint history, and result downloads. The demo does not use SSH, run scientific code, or allocate a GPU. Its checkpoints and metrics are synthetic and clearly labeled.

Demo state and its generated signing secret live in ignored `.state-demo/`. To preserve an existing demo, use a new `--state-dir` or restart it:

```bash
export DMASIF_WEBHOOK_SECRET_FILE="$PWD/.state-demo/webhook-secret"
dmasif-console --config .state-demo/config.yaml serve --host 127.0.0.1 --port 8000
```

The demo needs the development dependencies. A deployed service uses `requirements.lock`.

## What the service provides

| Component | Behavior |
| --- | --- |
| Signed push receiver | Repository, actor, and `runs/*` policy; durable acknowledgement and replay protection |
| Source preparation | Saves the exact commit and accepted settings; rejects unsafe archives, LFS, and submodules |
| Training adapter | One generated Slurm job invoking the submitted `src/run_experiment.py`; fixed prepared data and isolated outputs |
| Training dashboard | Latest step and all reported metrics; held-out evaluation and checkpoint history; exact-step filters and older-event paging |
| Durable telemetry | SQLite schema 2 stores event history independently of the bounded raw-log tail; full JSONL export |
| Validation | Checks required checkpoints, step/configuration evidence, held-out evaluation, and output checksums before success |
| Worker | One active application run, FIFO queue, restart recovery, and bounded monitoring/cache work |
| Operator controls | Pause/resume, cancellation, uncertain-submission reconciliation, backup, and restore |

Every run writes beneath `runs/<run_uuid>/jobs/<slurm_job_id>/training/` in the managed cluster root. Required outputs are `best.pt`, `last.pt`, `history.json`, `training-summary.json`, and `evaluation.json`. Retained `step<N>.pt` checkpoints and `evaluation-rows.tsv` are also validated and offered as downloads when present. Telemetry remains in the run directory and is available through the console's full metrics export. Large uncached outputs stay retained on the cluster.

Scheduler exit zero alone does not mean success. A run that stops at its training time budget retains checkpoints and final evaluation but is **Partial** if it did not reach the requested step target. Lost submission responses reserve capacity for reconciliation instead of automatically submitting another job.

## Operator setup

Use [config/training.example.yaml](config/training.example.yaml) and [docs/training-deployment.md](docs/training-deployment.md). The example intentionally has invalid identity/hash placeholders and `submissions_enabled: false`.

The training preset uses one GPU, eight CPUs, 96 GB RAM, and a 12-hour Slurm limit. The operator pins the runtime, adapter, fixed dataset files, initialization checkpoint, and approved GitHub actors. Researchers cannot override cluster paths, scheduler resources, runtime images, or dataset identity in their run YAML.

The application needs one always-on Python service, persistent SQLite/file storage, and a stable HTTPS webhook URL. Existing [Render hosting](docs/render.md) and [Docker Compose](docs/deployment.md) instructions describe the hosting options; use the training configuration and acceptance procedure when activating this release. Hosting the dashboard does not run training on the web server.

## Credentials and visibility

Private operator configuration, SSH keys, known-hosts files, repository tokens, and webhook secrets stay outside Git. Pass them through protected files or the deployment's secret manager. Docker contexts exclude local private state. Public responses redact configured cluster identities, paths, and secret values.

The dashboard, logs, and available downloads are public and read-only. Only signed pushes from approved numeric GitHub IDs can submit work; visitors cannot submit or cancel jobs. Keep credentials and private notes out of research code and logs. A shared cluster Unix account provides attribution and protection from accidental overwrites, not isolation from hostile code under that account.

## Verify

```bash
pytest -q
ruff check dmasif_console cluster_adapter tests
node --check dmasif_console/static/console.js
```

The optional DOM checks run with an existing jsdom installation:

```bash
JSDOM_MODULE=/path/to/node_modules/jsdom node tests/check_training_ui.cjs
```

These tests use local fixtures and fake scheduler commands. A successful local test suite is not a real training acceptance run. The [current architecture](ARCHITECTURE.md) separates the training design from the retained [October extraction acceptance record](docs/acceptance-2026-10-01.md).
