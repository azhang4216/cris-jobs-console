# Using the training console

**Start with the [dmasif-experiments README](https://github.com/azhang4216/dmasif-experiments#readme).** It explains how to create a branch from `main`, edit your code, test it, and push `runs/<experiment>`. For Nick's local code or another existing experiment, follow [Coming from an existing experiment](https://github.com/azhang4216/dmasif-experiments#coming-from-an-existing-experiment).

Work in the research repository and view results in the console. The operator provides the dashboard URL and approves your GitHub account; no SSH access or console-repository edits are needed. This release is local and unpublished; deployment and real GPU acceptance remain pending.

## What you can change

| Change | File in dmasif-experiments |
| --- | --- |
| Encoder architecture or pooling | `src/surface_encoder.py` and its helpers |
| Training logic or losses | `src/train.py` and its helpers |
| Batch size, step count, evaluation/save intervals, seed | `experiments/run.yaml` |
| Tests and explanation of your experiment | Relevant tests and documentation |

Commit every required helper. The console runs the exact pushed commit. New dependencies need an operator runtime update.

Preserve the `src/run_experiment.py` entrypoint/CLI, fixed dataset wiring, automatic held-out evaluation, required outputs, and telemetry format. These let the console run and monitor your changes. See the [training contract](https://github.com/azhang4216/dmasif-experiments/blob/main/docs/TRAINING_WORKFLOW.md).

## Data is already prepared

Training uses **`data/datasets/dataset_v2/`**: `index.parquet` describes the examples and `inputs.txt` identifies structures. The prepared item list, ligand features, surfaces, and PLIP caches are reused. No dataset selection, paths, or preparation commands are needed.

Evaluation uses the separate **`out/valset_v1v2/`** holdout: 400 rows across 240 target groups excluded from training. Periodic evaluation selects checkpoints; final evaluation of `best.pt` runs automatically. Shared data and caches stay read-only.

## Run settings

Edit the existing `experiments/run.yaml`:

| Setting | Default | Allowed values |
| --- | --- | --- |
| `batch_size` | `32` | Integer 2–128 |
| `steps` | `10000` | Integer 1–1,000,000 |
| `eval_every` | `125` | Evaluation interval; integer 1–1,000,000 |
| `save_every` | `125` | Checkpoint interval; integer 1–1,000,000 |
| `seed` | `0` | Integer 0–4,294,967,295 |
| `repeat_id` | `first-run` | 1–100 letters, numbers, underscores, dots, or hyphens |

Keep `schema_version: 1`, `job_type: dmasif_train`, `dataset_id: dataset_v2`, and `preset_id: training`. Cluster paths, arbitrary commands, and scheduler options are not accepted in this file. Batch size does not change the one-GPU allocation.

The defaults follow `src/slurm/train_v8.sbatch`. Researchers do not need to edit or submit Slurm files: the console generates one job for training and final evaluation.

## Read the dashboard

Open your run from the run list:

- **Training progress:** phase, step target, batch size, and every latest training metric.
- **Held-out evaluation / Checkpoints:** latest results and links to their histories.
- **Steps, evaluations & checkpoints:** filter by event type, jump to an exact step, or browse older records. **Download all metrics** exports the recorded history as JSONL.
- **Recent logs:** recent stdout/stderr. Scroll up to pause following; enable **Follow output** to return to new lines. Older structured metrics remain available after raw log lines leave this tail.
- **Saved outputs:** validated checkpoints and metric files available for download. Ask the operator for large uncached outputs using the run URL.

Updates are automatic; times use your browser's timezone. Delayed monitoring preserves the last known status.

| Status | Meaning |
| --- | --- |
| Checking / preparing / waiting | Saving source, checking inputs, or waiting for an application slot |
| Queued on cluster | Waiting for a GPU |
| Running | Executing training or evaluation; the current phase is shown separately |
| Checking results | Validating checkpoint and evaluation evidence |
| Succeeded | Requested step target reached and all required outputs validated |
| Partial | Training budget ended before the step target; checkpoints and evaluation are retained |
| Failed / timed out / cancelled | Read the reason and logs |
| Rejected / preparation failed | Correct the reported code/configuration issue before a new push |
| Submission uncertain / needs review | Ask the operator to check the existing job before repeating |

Required outputs are `best.pt`, `last.pt`, `history.json`, `training-summary.json`, and `evaluation.json`. Retained step checkpoints and per-row evaluation may also be available. A zero exit code or saved checkpoint alone does not establish success.

## Repeat a run or get help

Each qualifying push runs its final commit when submissions are enabled, with fresh outputs and no automatic resume. Reuse your branch for later changes; change `repeat_id` to repeat unchanged code. Duplicate webhooks do not create extra jobs. Leave old trigger-test branches untouched.

Documentation-only pushes to `runs/*` can also start jobs. For missing runs, check **Push activity** and the branch name. For cancellation or uncertain submissions, send the operator the run URL. Keep secrets out of commits and logs; the dashboard may be public.
