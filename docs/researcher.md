# Run a dMaSIF experiment

Use your normal Git workflow and the lab dashboard. You do not need an SSH login or a website account. The operator supplies the approved research repository, dashboard URL, and enabled dataset IDs. The dashboard's run details, logs, and result downloads are public; open the URL without signing in. Your personal GitHub account identifies and authorizes your submissions.

This first release performs **feature extraction** with the tested dMaSIF interface. Full training, arbitrary uploads, and new software dependencies need a later tested runtime/adapter release.

## Where to edit

**Make your experiment changes in the lab's dMaSIF research repository.** The `cris-jobs-console` repository provides the service and the copyable configuration template.

```text
your-dmasif-research-checkout/
├── experiments/
│   └── run.yaml          ← EDIT: dataset, seed, repeat label
├── affinity/
│   ├── extract.py        ← EDIT: the Python entrypoint that runs
│   └── my_experiment.py  ← OPTIONAL: your helper, called by extract.py
└── model.py              ← EDIT only if changing model behavior
```

Changing only run settings requires no Python edits. The files under `dmasif_console/`, `cluster_adapter/`, and `deploy/` are operator infrastructure; they are not experiment entrypoints.

## First run

Develop in the lab's approved research repository. Save this file as `experiments/run.yaml`:

```yaml
# EDIT THESE:
dataset_id: demo-1stp-v1  # Choose a registered dataset from the operator.
seed: 0
repeat_id: first-run

# KEEP THESE:
schema_version: 1
job_type: dmasif_extract
preset_id: quick-test
```

Commit your intended code changes and configuration, then push a branch named `runs/experiment-name`. Replace `pocket-v1` with your experiment name:

```bash
git switch -c runs/pocket-v1
# Include other Python files you changed or added, if any:
git add experiments/run.yaml affinity/extract.py
git diff --cached
git commit -m "Run pocket-v1 feature extraction"
git push -u origin HEAD
```

Include any new helper files in `git add`; only files in the pushed commit reach the cluster. **Pushing the run branch submits the job.** Saving a file or making a local commit does not submit it.

Use your own authorized GitHub account. The dashboard automatically records who pushed from the signed GitHub webhook, including their login and numeric ID. It records the commit author separately. You do not need to put your username in the branch name.

Branch names are shared within the repository. Choose different experiment names for independent work. Any approved researcher can push to a shared run branch; each qualifying push is attributed to the person who pushed and gets separate outputs. You may include your username in an experiment name for convenience, but it does not control attribution.

Open the dashboard to follow the run. One push creates one run for its final commit, even when the push contains several commits. Only committed files are included. Creating a run branch or force-pushing one can start work. Every push to a run branch qualifies, including a documentation-only commit; use ordinary development branches until ready to run.

## Changing Python code

The runner always starts `affinity/extract.py`. To use another Python file, add it to the research repository and import/call it from that extractor. Committing a standalone `train.py` or adding `script: train.py` to the YAML does not select a new entrypoint; unsupported YAML fields are rejected.

Preserve the extractor's existing interface:

| Supplied argument | Responsibility |
| --- | --- |
| `--inputs` | Read the service's input list. |
| `--out` | Write results here. This directory is unique to the run and scheduler job. |
| `--repo` | Use this pinned source checkout; it is mounted read-only. |
| `--ckpt` | Load this absolute checkpoint path. Model changes must remain compatible with it. |
| `--seed`, `--device` | Use the chosen seed and allocated GPU. |
| `--max_atoms`, `--max_proteins`, optional `--merge_models` | Preserve the preset limits and dataset behavior. |

Keep the existing NPZ filenames and schema, including 16-dimensional `input_feats`, `emb1`, and `emb2`, coordinates, normals, and atom metadata. The service validates these before publishing results. Use `--out` throughout the extractor and any custom helper instead of a fixed shared output path.

The container supplies installed dependencies; editing a requirements file does not install new packages. The service also generates the Slurm script, so edits to the research repo's `affinity/slurm/extract_test.sbatch` do not affect these submissions. Arrange new dependencies, full training, checkpoint changes, or a different result format with the operator.

## Run configuration fields

| Field | Researcher setting |
| --- | --- |
| `dataset_id` | Operator-registered dataset ID; use the enabled ID for your data. |
| `seed` | Integer from `0` to `4294967295`. |
| `repeat_id` | A label of 1–100 letters, numbers, underscores, dots, or hyphens; change it for an intentional repeat. |
| `schema_version` | Keep `1`. |
| `job_type` | Keep `dmasif_extract`. |
| `preset_id` | Keep `quick-test`. |

`dataset_id` uses the same 1–100 character alphabet as `repeat_id`. Extra fields, including script names, arbitrary commands, and output paths, are rejected.

## What is saved

Each run has a unique ID, retained source snapshot, full commit link, configuration, dataset identity, and output checksums. The dashboard records receipt/submission/start/end times, scheduler status, logs, and available validated downloads.

Runs always use separate output directories. Repeating a commit never overwrites earlier output. A green “Succeeded” status means the scheduler finished successfully **and** all expected results passed validation. An empty output directory does not count as success.

## Repeat or change a run

**You can reuse the same `runs/experiment-name` branch.** Pushing a new commit creates a new run ID, saved source snapshot, and output directory; earlier runs keep their code, logs, and results. This protection does not depend on changing `repeat_id`. A force-push also creates a separate run, although ordinary commits are easier to trace.

Update the code/configuration as needed. To repeat unchanged code, change `repeat_id` to a new value, commit, and push:

```yaml
repeat_id: second-run
```

This starts a new run. It does not resume the previous run. Dataset IDs come from the operator's immutable registry; cluster paths, custom shell commands, image paths, and scheduler flags are not accepted in the experiment file.

A push with no Git changes does not create a new event. GitHub redelivery of an already accepted event reuses the existing run and cannot submit it twice. One push runs its final commit, not every intermediate commit. If a force-push makes a commit unavailable before the service can save it, preparation fails rather than substituting different code.

## Reading the dashboard

| Status | Meaning |
| --- | --- |
| Checking request | Saving the exact commit and validating its configuration |
| Waiting for app slot | Accepted; another application run holds the single slot |
| Queued on cluster | Submitted to the scheduler and waiting for a GPU |
| Running | The scheduler confirms that execution started |
| Checking results | Execution finished; output evidence is being checked |
| Succeeded | All expected outputs passed validation |
| Partial / failed | Some or all expected results are missing, invalid, or execution failed |
| Rejected / preparation failed | Read the reason, correct the configuration/code, then make a new push |
| Submission uncertain / needs review | Ask the operator to resolve the evidence before repeating |

The page refreshes automatically. Times use your browser's timezone, shown in the footer. A stale observation preserves the last known job state; a temporary connection failure does not imply your experiment failed.

The log panel shows recent output. Scroll upward to stop following; check **Follow output** to return to new lines. Small validated outputs appear as downloads. If a verified result is too large or no longer cached, ask the operator to retrieve it; an unavailable download does not change a successful scientific outcome.

## Missing runs, cancellations, and help

Check the `runs/experiment-name` branch convention and expand **Push activity** below the run table. A non-run branch is ignored; an unapproved GitHub account is rejected. If no event appears, contact your lab operator to check GitHub delivery history. A missing event does not prove that the push failed or that a job is running.

For cancellation, send the operator the run URL/UUID. You never need to obtain cluster credentials. Do not create repeated pushes to fix an uncertain submission; the operator must determine whether the original job already exists.

Keep the approved extractor arguments and result schema intact. If an experiment needs new packages, a different feature format, additional datasets, or training, arrange a tested runtime/adapter update with the operator before submitting.
