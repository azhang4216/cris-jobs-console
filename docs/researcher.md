# Run a dMaSIF experiment

Use your normal Git workflow and the lab dashboard. You do not need an SSH login or a website account. The operator supplies the approved research repository, dashboard URL, and enabled dataset IDs. The dashboard's run details, logs, and result downloads are public; open the URL without signing in. Your personal GitHub account identifies and authorizes your submissions.

This first release performs **feature extraction** with the tested dMaSIF interface. Full training, arbitrary uploads, and new software dependencies need a later tested runtime/adapter release.

## First run

Develop in the lab's approved research repository. Save this file as `experiments/run.yaml`:

```yaml
schema_version: 1
job_type: dmasif_extract
dataset_id: demo-1stp-v1
preset_id: quick-test
seed: 0
repeat_id: first-run
```

Commit your intended code changes and configuration, then push a branch named `runs/experiment-name`. Replace `pocket-v1` with your experiment name:

```bash
git switch -c runs/pocket-v1
git add experiments/run.yaml affinity/extract.py
git commit -m "Run pocket-v1 feature extraction"
git push -u origin HEAD
```

Use your own authorized GitHub account. The dashboard automatically records who pushed from the signed GitHub webhook, including their login and numeric ID. It records the commit author separately. You do not need to put your username in the branch name.

Branch names are shared within the repository. Choose different experiment names for independent work. Any approved researcher can push to a shared run branch; each qualifying push is attributed to the person who pushed and gets separate outputs. You may include your username in an experiment name for convenience, but it does not control attribution.

Open the dashboard to follow the run. One push creates one run for its final commit, even when the push contains several commits. Only committed files are included. Creating a run branch or force-pushing one can start work. Every push to a run branch qualifies, including a documentation-only commit; use ordinary development branches until ready to run.

## What is saved

Each run has a unique ID, retained source snapshot, full commit link, configuration, dataset identity, and output checksums. The dashboard records receipt/submission/start/end times, scheduler status, logs, and available validated downloads.

Runs always use separate output directories. Repeating a commit never overwrites earlier output. A green “Succeeded” status means the scheduler finished successfully **and** all expected results passed validation. An empty output directory does not count as success.

## Repeat or change a run

Update the code/configuration as needed. To repeat unchanged code, change `repeat_id` to a new value, commit, and push:

```yaml
repeat_id: second-run
```

This starts a new run. It does not resume the previous run. Dataset IDs come from the operator's immutable registry; cluster paths, custom shell commands, image paths, and scheduler flags are not accepted in the experiment file.

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
