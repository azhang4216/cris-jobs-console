# CRIS job console

This is the **operator's repository** for the web dashboard and job service.

**To run an experiment, use the [dmasif-experiments README](https://github.com/azhang4216/dmasif-experiments#readme).** Researchers edit code and settings there, push a `runs/*` branch, and follow the run in the web console. They do not need to edit this repository or log in to the VM.

**Release status:** this repository contains the training integration. Operator deployment and validation with a real GPU training submission are still pending. The VM inspection was read-only. See the [training deployment guide](docs/training-deployment.md) for activation and GPU acceptance steps.

## What the console does

A signed push saves the exact research commit and starts one Slurm job when submissions are enabled. That job trains the model, evaluates its best checkpoint, and saves its outputs. The dashboard shows live status, every reported step metric, evaluation history, checkpoints, and recent logs. Researchers can filter by step or event type and download the full metrics history.

| Researchers change in dmasif-experiments | The workflow keeps fixed |
| --- | --- |
| Architecture/pooling in `src/surface_encoder.py`, losses in `src/train.py`, and their helpers | Prepared training data at `data/datasets/dataset_v2/` |
| Batch size, steps, evaluation/save intervals, seed, and repeat label in `experiments/run.yaml` | Separate held-out evaluation at `out/valset_v1v2/` |
| Tests and documentation for their research change | Wrapper CLI, automatic final evaluation, required outputs, and telemetry format |

The existing dataset, prepared inputs, and caches are reused read-only. Each run gets a separate output directory and starts fresh. Required outputs are `best.pt`, `last.pt`, `history.json`, `training-summary.json`, and `evaluation.json`. A run is successful only after its requested step target and outputs validate; stopping at the training time budget is **Partial**.

For Nick's existing experiments, use [Coming from an existing experiment](https://github.com/azhang4216/dmasif-experiments#coming-from-an-existing-experiment). The [console guide](docs/researcher.md) explains settings, progress, and results.

## Preview locally

Requires Python 3.12+ and Git:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
dmasif-console demo --serve
```

Open **http://127.0.0.1:8000**. The demo shows two simulated training runs; it does not use SSH, scientific code, or GPUs. Its state lives in ignored `.state-demo/`. To keep an existing demo, choose a new directory with `--state-dir .state-demo-2`.

## Operate the service

Start with [training deployment](docs/training-deployment.md) and [config/training.example.yaml](config/training.example.yaml). The example deliberately contains invalid placeholders and leaves submissions disabled. The default training allocation is one GPU, eight CPUs, 96 GB RAM, and 12 hours.

The operator configures approved GitHub accounts, fixed data/runtime identities, cluster access, and an always-on service with persistent storage. Keep real credentials and site configuration outside Git. Dashboard logs and available downloads are public and read-only; large outputs may remain on the cluster when they exceed download-cache limits.

See [architecture](ARCHITECTURE.md) for execution, telemetry, and recovery details; [Render](docs/render.md) or [Compose](docs/deployment.md) for hosting. Earlier extraction acceptance records are historical and do not validate this training release.

## Verify locally

```bash
pytest -q
ruff check dmasif_console cluster_adapter tests
node --check dmasif_console/static/console.js
```

Optional browser-DOM checks: `JSDOM_MODULE=/path/to/node_modules/jsdom node tests/check_training_ui.cjs`. These checks use local fixtures, not real cluster jobs.
