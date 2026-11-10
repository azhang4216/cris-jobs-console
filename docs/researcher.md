# Run a training experiment

Work in [dmasif-experiments](https://github.com/azhang4216/dmasif-experiments#readme), then watch your run in the lab's job console. You do not need an SSH login, a console account, or changes to `cris-jobs-console`. The operator supplies the dashboard URL and approves your GitHub account for submissions.

Training always uses the existing **dataset_v2**. Evaluation is separate: the existing `valset_v1v2` holdout contains 400 rows across 240 target groups, which are excluded from training. Dataset preparation, surface caches, and PLIP data are already present; no researcher data-selection or rebuilding step is needed.

## Make and submit a change

Start a new branch from the current research `main`. Old webhook trigger-test branches can stay untouched.

```bash
git switch main
git pull --ff-only
git switch -c runs/my-encoder
# Edit research code and/or experiments/run.yaml.
git add src/surface_encoder.py experiments/run.yaml
# Include every other file your change needs.
git diff --cached
git commit -m "Try my encoder change"
git push -u origin HEAD
```

| What to change | File in dmasif-experiments |
| --- | --- |
| Protein encoder, architecture, or pooling | `src/surface_encoder.py` and its helpers |
| Training computation or losses | `src/train.py` |
| Batch size, step count, evaluation/save cadence, seed | `experiments/run.yaml` |
| Research preset or final evaluation behavior | `src/run_experiment.py` |

Include every required helper in your commit. The console saves and executes that commit's code; edits left only on your laptop are not submitted. New dependencies require an operator runtime update.

Each new qualifying push to `runs/<experiment>` submits its final commit when submissions are enabled. Every run receives a new ID, source snapshot, output directory, and logs. A duplicate webhook reuses the existing run. Pushing multiple commits together submits the final commit once. A documentation-only push to a run branch can also start a job, so develop on an ordinary branch until ready.

Your GitHub account identifies who pushed. Use distinct branch names for independent work; a branch name does not establish ownership or authorization. You may reuse your experiment branch for later changes. To repeat identical code, change `repeat_id`, commit, and push. New runs start fresh and do not automatically resume earlier runs.

## Configure the run

The research repository already contains `experiments/run.yaml`:

```yaml
schema_version: 1
job_type: dmasif_train
dataset_id: dataset_v2
preset_id: training
batch_size: 32
steps: 10000
eval_every: 125
save_every: 125
seed: 0
repeat_id: first-run
```

| Setting | Meaning |
| --- | --- |
| `batch_size` | Examples per training batch; integer 2–128. The v8 default is 32. |
| `steps` | Requested training step target; default 10,000. |
| `eval_every` | Periodic held-out evaluation interval in steps; default 125. |
| `save_every` | Checkpoint interval in steps; default 125. |
| `seed` | Integer random seed, 0–4,294,967,295. |
| `repeat_id` | Label using letters, numbers, underscores, dots, or hyphens; up to 100 characters. |

Keep `schema_version`, `job_type`, `dataset_id`, and `preset_id` as shown. Step counts and intervals must be positive integers, at most 1,000,000. Arbitrary script names, shell commands, cluster paths, and scheduler flags are not accepted in this file. Batch size does not change the one-GPU allocation.

The scientific defaults come from the existing `src/slurm/train_v8.sbatch` preset; the inspected project has no `train_b8.sbatch`. You do not need to edit or submit Slurm files. The console generates one job and calls the submitted `src/run_experiment.py`, which runs training and then evaluates the best checkpoint automatically.

## Follow the run

The console's run list shows researcher, commit, training progress, status, and runtime. Open a run for:

- **Training progress:** current phase, step target, batch size, and every metric reported by the latest training step.
- **Held-out evaluation:** latest evaluation metrics and a direct link to evaluation history. These measurements are separate from training updates.
- **Checkpoints:** saved checkpoint names and steps, with a link to the complete checkpoint event history.
- **Steps, evaluations & checkpoints:** filter by event type, jump to an exact step, or page through older records. All metric fields are retained. **Download all metrics** exports the complete recorded history as JSONL.
- **Recent logs:** a bounded stdout/stderr tail. Scroll up to stop following; enable **Follow output** to return to new lines. Earlier structured metrics remain available even after their raw log lines leave this tail.
- **Saved outputs:** validated checkpoints and metric files that are available in the download cache. Uncached large outputs remain on the cluster; contact the operator with the run URL to retrieve them.

The page updates automatically and uses your browser's timezone. Delayed monitoring preserves the last known state; it does not imply failure. Older extraction tests remain viewable with their original logs and results, without invented training metrics.

## Outputs and completion

Required outputs are `best.pt`, `last.pt`, `history.json`, `training-summary.json`, and `evaluation.json`. It also writes per-row final evaluation and append-only telemetry inside its own output directory. Training includes periodic held-out evaluation for checkpoint selection; the final evaluation reads `best.pt` and performs no optimization.

| Status | Meaning |
| --- | --- |
| Checking request / preparing | Saving the exact source and validating fixed inputs and settings |
| Waiting for app slot | Another application run holds the execution slot |
| Queued on cluster | Slurm is waiting for resources |
| Running | The job is executing; training/evaluation phase is shown separately |
| Checking results | The service is validating checkpoint and evaluation evidence |
| Succeeded | Requested step target reached and every required output validated |
| Partial | Training time budget ended before the step target; retained checkpoints and held-out evaluation remain available |
| Failed / timed out / cancelled | The job did not finish successfully; inspect the reason and logs |
| Rejected / preparation failed | Correct the reported code/configuration issue, then make a new push |
| Submission uncertain / needs review | Ask the operator to reconcile the existing job before repeating |

A zero process exit code or a saved checkpoint alone does not count as a completed experiment. Step metrics follow successful optimizer updates; the trainer can skip unusable batches while its scheduled step index advances. `optimizer_updates` distinguishes those counts in the retained completion evidence.

For missing submissions, check the branch name and **Push activity** below the run table. For cancellation or an uncertain submission, contact the operator with the run URL. Researchers do not need cluster credentials. Dashboard details and logs may be visible to others, so keep secrets and private notes out of commits and output.
