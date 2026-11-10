# Activate the training workflow

This guide is for the operator. Researchers use [dmasif-experiments](https://github.com/azhang4216/dmasif-experiments#readme) and the web console; they do not deploy this service or choose dataset paths.

**Status at November 10, 2026:** the VM was inspected read-only. The training adapter, researcher wrapper, and dashboard changes are local implementations. No adapter installation, remote configuration change, dataset rebuild, deployment, or real training job was performed for this update. Activation requires a pinned release and an approved GPU acceptance run.

## Scientific baseline

The current cluster research project is based on `cszcz00/dMaSIF`, branch `claude/model-training`, commit [`2665d9d01c285c2cdffdff0e4414860bd714e83e`](https://github.com/cszcz00/dMaSIF/commit/2665d9d01c285c2cdffdff0e4414860bd714e83e). Its documented current preset is `src/slurm/train_v8.sbatch`; no `train_b8.sbatch` was found.

The researcher repository carries this pipeline, its documentation, and a fixed `src/run_experiment.py` wrapper. The wrapper preserves the v8 scientific defaults, accepts bounded run settings, emits structured telemetry, and automatically evaluates the best checkpoint after training. Nick's `nick-local-testing` checkout is a set of local experiment snapshots and optional pooling patches. Its alternate poolers are not silently substituted for the cluster baseline.

The default run uses batch 32, 10,000 steps, evaluation/save intervals of 125, and the existing predicted-pocket/pair-pooling/learned-atom-feature/PLIP configuration. One generated Slurm job invokes the submitted wrapper. The researcher-facing `src/slurm/train.sbatch` uses the same wrapper; researchers using the console do not submit it themselves.

## Fixed data registration

Copy [config/training.example.yaml](../config/training.example.yaml) into ignored operator configuration. Fill in the real repository ID, approved numeric actor IDs, cluster settings, runtime identity, and SHA-256 values. The placeholders intentionally fail validation. Keep `submissions_enabled: false` during preparation.

The dataset record must be named `dataset_v2` and use `kind: training`. `project_root` points to the existing prepared project. Register these eight identities, using paths relative to that root:

| Hash key | Existing file |
| --- | --- |
| `index` | `data/datasets/dataset_v2/index.parquet` |
| `inputs` | `data/datasets/dataset_v2/inputs.txt` |
| `items` | `out/runs/dataset_v2/items.tsv` |
| `ligand_feats` | `out/runs/dataset_v2/ligand_feats.npz` |
| `val_set` | `out/valset_v1v2/val_set.tsv` |
| `holdout_groups` | `out/valset_v1v2/holdout_groups.txt` |
| `val_provenance` | `out/valset_v1v2/provenance.json` |
| `head` | `out/encoder_test/head_pretrained_300.pt` |

`index.parquet` describes the dataset and `inputs.txt` lists the structure inputs. Training consumes the already prepared `items.tsv` and `ligand_feats.npz`; it does not regenerate them from the index. The separate held-out set has 400 rows across 240 target groups. Those target groups are excluded from training and used for periodic and final evaluation.

The existing `data/surfaces/`, `data/pockets/fpocket/`, `data/plip/`, and `systems/` directories must be available. Surfaces must already contain the required current atom features. Registration verifies the eight files above; it is not a byte-by-byte manifest of every shared cache file.

`source_prefix` is the original absolute root embedded in `items.tsv`, if it differs from `project_root`. Staging checks those paths and writes a normalized **per-run copy**, replacing that prefix with `/dataset`. It records both the original and normalized hashes. The shared manifest is never edited.

At execution, the prepared project is mounted at `/dataset:ro`, with the normalized item list overlaid read-only. Source is mounted at `/source:ro`. `CRIS_READONLY_DATA=1` makes missing or incompatible caches fail instead of rebuilding them. Run outputs and runtime compilation caches use separate writable run directories. This workflow does not prepare datasets, run PLIP, regenerate surfaces, or mutate shared caches.

## Runtime and resource policy

Use `presets.training.kind: training`. The default allocation is **one GPU, eight CPUs, 96 GB RAM, 12 hours**, with `qos: null` rather than extraction's short `test` QoS. A site-specific QoS or GPU type belongs in operator configuration. The trainer's default budget is 11 hours, leaving time for final evaluation and publication inside the 12-hour allocation. Smaller operator wall limits reserve a proportional evaluation window.

The inspected working sandbox reports **PyTorch 2.8.0a0, NVIDIA 25.06, CUDA 12.9**. Existing training logs show Blackwell execution with that environment. The old extraction image and its October GPU failures are not equivalent evidence for this newer stack.

A read-only hash of the existing `dmasif.sif` was:

```text
c96318c57c068160281ec2f0d95e0d6b55c2bfa8ca46d284d001f213ee1a552f
```

This identifies that file; it does **not** establish that its installed dependencies match the working sandbox or that it passes the new wrapper's training/evaluation path. Validate the immutable training image on the actual allocated GPU before enabling routine submissions. A requested GPU type can differ from the allocation under site scheduler policy, so retain the allocation and kernel/runtime evidence. If the site needs `runtime_unsquash`, enable it deliberately and provide sufficient node-local scratch.

The training initialization checkpoint is resolved from the registered `head` file and hash. A training-only configuration does not need the old extraction `cluster.checkpoint_path` fields. The adapter still pins the resolved checkpoint as part of each accepted run.

## Deploy a pinned release

1. Back up the current application state, keep submissions disabled, and prepare the updated research `main` for researchers. Leave old test branches and their run records intact.
2. Install an immutable adapter release containing `__init__.py`, `common.py`, `helper.py`, `batch.py`, `execute.py`, `validate.py`, and `training.py`. Use a new release directory and point `cluster.helper_path` and `adapter_version` to it. Do not overwrite a release used by accepted runs.
3. Pin the approved training SIF path/hash and the registered data identities in private operator configuration. Supply SSH/known-hosts, repository-token, and webhook-secret files through the application host's runtime configuration. No real host, account, paths, or credentials belong in tracked examples.
4. Deploy the web process and one worker with persistent state. Store initialization upgrades SQLite to schema **2** using `002_training_events.sql`; back up the database before upgrading. Existing extraction records remain readable.
5. Confirm the signed webhook targets the research repository, the approved numeric GitHub IDs are correct, and only `runs/*` pushes qualify. Browser requests read cached local state; the dataset discovery endpoints have been removed.
6. After approval, run the bounded GPU acceptance described below. Enable routine submissions only after its evidence is complete.

The existing [hosting guide](render.md) and [Compose guide](deployment.md) cover service hosting, backups, and secrets. Their extraction examples and the old [adapter guide](cluster-adapter.md) describe the earlier release; this document and `training.example.yaml` define training-specific settings.

## Acceptance evidence

Use an approved short run with a small step target and suitable evaluation/save intervals. It still exercises the fixed held-out evaluation, which needs time even for a short training segment. Do not reuse a historical run output or shared upstream training directory.

Verify that one push creates one run and one Slurm submission, with the exact source commit and chosen batch size. Confirm queued/running states, successful optimizer-step events, periodic evaluation, checkpoint events, final best-checkpoint evaluation, and all five required outputs. Exercise telemetry polling across worker restart and repeated delivery without a second job. Confirm the shared input hashes remain unchanged and no cache preparation occurred.

A successful result requires validated `best.pt`, `last.pt`, `history.json`, `training-summary.json`, and `evaluation.json`. The checkpoint/configuration/step evidence must agree, and the final evaluation must identify `best.pt` and `valset_v1v2` with finite metrics. If the trainer stops at its time budget, `budget_exhausted` produces **PARTIAL**, even when the process exits zero and outputs validate. Scheduler failures, timeout, or cancellation retain their corresponding outcome.

The default download cache is intentionally bounded. Checkpoints beyond its per-file/run limits remain on the cluster and are shown as unavailable for download; that does not change scientific success. Size the application's cache and disk deliberately if all checkpoint downloads are needed.

## Telemetry and output layout

```text
<managed-root>/
├── releases/sources/<archive-sha>/tree/  # exact submitted source snapshot
└── runs/<run-uuid>/
    ├── inputs/items.tsv                # normalized per-run copy
    ├── logs/
    └── jobs/<slurm-job-id>/
        ├── training/
        │   ├── best.pt
        │   ├── last.pt
        │   ├── history.json
        │   ├── training-summary.json
        │   ├── evaluation.json
        │   ├── evaluation-rows.tsv
        │   └── telemetry.jsonl
        └── published/                  # validated required and optional output copies
```

The wrapper runs from the fixed data root with `--no_auto_resume` and imports research modules from the submitted snapshot. Every new run has a fresh output directory. The generated Slurm file controls resources and execution once; there is no separate evaluation submission.

Telemetry events use **event schema 1**, independently of **SQLite schema 2**. They contain sequence, timestamp, kind, step, total steps, and arbitrary named numeric metrics. The worker reads bounded append-only chunks. Event inserts, the byte offset, and the latest summary commit atomically, making restart/replay safe. Step/evaluation/checkpoint history remains in the `training_events` table independently of the bounded raw log tail.

The read-only API provides filtered, paginated history at `/api/runs/{id}/training-events` and complete JSONL export at `/api/runs/{id}/training-events/download`. The UI shows latest values, exact-step search, and older-event navigation without replacing the page a researcher is reading. This is the primary interface for detailed training visibility; raw logs remain useful for diagnostics.
