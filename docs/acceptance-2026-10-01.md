# Real submission acceptance record — October 1–2, 2026

**Status: real feature-extraction acceptance passed locally and publicly.** The valid push progressed through waiting, queued, running, validation, and success states. Job `114515` completed on H100 with exit `0:0` in **101 seconds**, producing a checksum-verified `1STP.npz`. Two H100 GPUs were allocated; extraction used one. The invalid configuration test remains correctly rejected with no scheduler job. The first two Blackwell failures remain visible with their original evidence. This verifies the requested submission/results workflow, not full training or permanent hosting. Further submissions remain paused because the two-GPU reservation was authorized for this test only.

## Setup tested

- Initial infrastructure: [cris-jobs-console, `02d5b99`](https://github.com/azhang4216/cris-jobs-console/commit/02d5b99); subsequent verified fixes and the active adapter are recorded below.
- Private research repository: [dmasif-experiments](https://github.com/azhang4216/dmasif-experiments), based on `cszcz00/dMaSIF` `modern-stack` commit `a56a69f16ea97dd3bb59d91cdcf9a298c412ce90`.
- One supervised local application and worker, SQLite, a signed GitHub push webhook, and a temporary public HTTPS tunnel.
- One immutable `1STP` input, registered checkpoint, and frozen Apptainer image. The first run retains execution adapter `v1-7f73a00d9461`; the first corrective release is `v1-c1ed82636117`, and the successful October 2 run uses `v1-b419bd4bd0e7`. The runtime image is unchanged.
- Initial attempts: one GPU, eight CPUs, 64 GB memory, 15-minute limit. The first H100 retry was rewritten to Blackwell. The later authorized test requested two typed H100 GPUs and used one; actual GPU execution and output validation passed.
- Approved GitHub sender: `azhang4216`, numeric ID `64555467`. No other researchers are authorized yet.

## Real pushes

| Case | Branch and exact commit | Run | Observed outcome |
| --- | --- | --- | --- |
| First extraction | `runs/1stp-smoke-check`, [`74f070320047e0b6f00cedeb1737c3ebdcb075ee`](https://github.com/azhang4216/dmasif-experiments/commit/74f070320047e0b6f00cedeb1737c3ebdcb075ee) | `641eb5c0-2770-45ce-b4dc-58b1fa0bdc89` | Queued → running → failed. Slurm job `111423`, exit `1:0`, duration 179 seconds. No validated results. |
| Invalid configuration | `runs/config-rejection-check`, [`7a8ac87600b5fbf089bb77d7dc3e00bbc4d452a6`](https://github.com/azhang4216/dmasif-experiments/commit/7a8ac87600b5fbf089bb77d7dc3e00bbc4d452a6) | `7388ff52-0210-4904-9ab9-a235108ab2c9` | Rejected with a field-specific error; zero scheduler jobs and zero artifacts. |
| H100 retry | `runs/1stp-smoke-check-h100`, [`a4c4a819c0b7500019fc8ff8596c4aa042cd883b`](https://github.com/azhang4216/dmasif-experiments/commit/a4c4a819c0b7500019fc8ff8596c4aa042cd883b) | `989aecfe-7f8b-4f62-9cb9-26757ebc3bc2` | Slurm job `112108` failed, exit `1:0`, duration 194 seconds. H100 requested; scheduler allocated Blackwell. No validated results. Original failed run and its output directory retained. |
| Authorized two-GPU H100 test | `runs/1stp-smoke-check`, [`347623c02d64e440bc4ef61cbce787a95b923531`](https://github.com/azhang4216/dmasif-experiments/commit/347623c02d64e440bc4ef61cbce787a95b923531) | `cebcd3be-6636-41c9-82d7-49ce1e61b38e` | Slurm job `114515` completed with exit `0:0`, duration 101 seconds. Two H100 GPUs requested and allocated; one used. `1STP.npz` passed local/public validation and download checks. The original branch's earlier run remains separate. |

The first extraction and invalid-configuration GitHub deliveries received HTTP 202. The first valid job was submitted at **2026-10-01 01:59:30 UTC**. The invalid request used a string instead of an integer for `seed`; the site reports:

> Invalid experiments/run.yaml. experiments/run.yaml.seed: Must be an integer from 0 to 4294967295 (without quotes). No cluster job was submitted.

The rejected request still retains its exact actor, branch, commit link, source checksum, and validation report. It does not consume a GPU allocation.

## First extraction failure and status recovery

Slurm records the original job's start at **2026-10-01 08:16:49 UTC** and end at **08:19:48 UTC**, with state `FAILED` and exit code `1:0`. It executed for **179 seconds**, not nine hours. PyTorch warned that the allocated RTX PRO 6000 Blackwell GPU (`sm_120`) was unsupported by the frozen build, then KeOps failed with `CUDA_ERROR_INVALID_IMAGE`. The corrective request asked for an H100 with the existing runtime and a new run identity; that request did not guarantee the hardware actually allocated.

The dashboard missed completion because its accounting parser required the UUID comment that this site does not store after jobs finish. It discarded the real failure row and extrapolated runtime from the last known running state. The fix accepts empty accounting comments while still checking the owner, full UUID job name, and exact requested ID. Conflicting comments remain rejected. Read-only monitoring uses the newly installed helper only when host, user, and managed root match; the original run's accepted policy, source, runtime, and execution adapter identities were verified unchanged.

The application was backed up, upgraded, and reconciled through its operator workflow. Its API now reports `FAILED`, 179 seconds, the correct end time, a cleared monitoring error, and a released capacity slot. Actual Chrome checks confirmed matching local/public history and detail views, retained CUDA logs, correct timestamps, and no mobile overflow or JavaScript errors. Stale monitoring now labels a state **Last known** and freezes its timer at the last successful observation.

The corrective implementation passes **237 automated tests**, Ruff, and JavaScript syntax checks. Regression coverage includes missing accounting comments, incorrect/conflicting job identities, read-only helper upgrades, typed GPU requests, recovered failures, and stale timer expiry/recovery in the API and browser. The existing GitHub webhook now points to the active public tunnel; its secret was preserved.

## H100 retry: scheduler policy confirmed read-only

Job `112108` started at **2026-10-01 17:57:55 UTC** and ended at **18:01:09 UTC**, with state `FAILED`, exit `1:0`, and duration **194 seconds**. The dashboard API reports the same terminal state and timestamps, with no stale monitoring flag.

The accepted policy, staged request, and saved `submit.sbatch` all requested `gpu:nvidia_h100_80gb_hbm3:1`. However, `scontrol` recorded an untyped `TresPerNode=gres:gpu:1` request and `Features=rtx6000`; accounting reported `gres/gpu:rtx_pro_6000_blackwell=1`. The log independently identified the Blackwell device. This was a real allocation change, not just a misleading run name or container GPU index.

The installed Slurm configuration enables the Lua submission plugin. Reading its `job_submit.lua` confirmed that it deliberately rewrites typed single-GPU requests to the RTX pool, overrides conflicting placement constraints, and emits a user warning. The console's existing helper discarded standard error after a successful `sbatch`, so that warning was lost. No scheduler configuration, source, image, job, or output was changed during this investigation; no new job was submitted.

Comparison with the earlier manual jobs confirmed that `109997` ran successfully on H200 and `110845` on H100. Both accounting records show one allocated GPU, and both logs report a real `1STP` extraction. The original software therefore worked on those devices; the console failures used different hardware. The current policy source does not establish why the earlier single-GPU jobs were routed differently, and no policy-change history has been verified.

At this stage, the lab chose **one H100/H200 GPU with the existing runtime** and paused submissions pending a permitted route. The initial October 2 decision declined reserving two GPUs; the later authorization and new test are recorded below. Blackwell support remains deferred in [TODO.md](../TODO.md). A local guard or a typed directive cannot change the cluster's allocation policy. Adapter improvements apply to future runs only.

Local follow-up changes retain submission warnings, redact them in run activity, preserve the scheduler's GPU visibility mask, and stop known model mismatches before extraction. **287 local automated tests passed** at that revision, including simulated H100-to-Blackwell routing, warning recovery/privacy, and same-branch output preservation checks. These are software regression tests, not a successful real GPU run. Immutable helper release `v1-758b73ffec9c` was installed during that test preparation but was not activated. A later release is now active as recorded below. The read-only acceptance verifier correctly returns failure for job `112108`.

The optional Blackwell container build (`112184`) was cancelled while pending when the lab chose to keep H100/H200. It never started, allocated no GPU, and produced no adopted image. The existing runtime remains unchanged. Blackwell-specific checks were removed from the prepared research branch before any new experiment push.

## October 2: one-GPU route rechecked

The lab requested another passing test and confirmed **one GPU only; wait for an H100/H200 route**. Read-only SSH inspection found the routing policy unchanged. Both `sbatch --test-only` requests—one typed H100 GPU and one typed H200 GPU, each with the existing account, partition, test QoS, eight CPUs, 64 GB memory, and a 15-minute limit—returned the explicit warning that the requested GPU type would be rewritten and routed to the RTX 6000 pool. These feasibility checks submitted no jobs.

The configured partitions did not demonstrate an exempt H100/H200 route. No new experiment commit was pushed, no new extraction was submitted, and the local API still confirmed submissions paused with the same three recorded runs. The existing runtime and one-GPU preset were retained.

The remaining question for routine one-GPU use is: **What permitted partition/QoS/reservation or policy exception lets this account request exactly one H100 or H200 for a 15-minute test without rewriting it to RTX 6000?** Obtain the exact supported submission directives, then verify the actual allocation and extraction results. A dry-run acceptance or GPU name alone does not establish scientific success.

## October 2: authorized two-GPU test on the original branch

The user later authorized the test needed to exercise a real push and run, superseding the earlier decision to wait. The agreed scope is one isolated reservation of **two H100 GPUs, with the existing extractor using one**. The new operator setting accepts only integer counts one or two, defaults to one, and requires an explicit supported H100/H200 type for count two. The approved test retains the existing runtime, input, and checkpoint. Blackwell support remains deferred; unrelated cluster changes are outside this test authorization.

Immutable helper `v1-b419bd4bd0e7` was installed and activated. The implementation passed **334 local automated tests** before deployment. These cover strict operator resource limits, accepted-policy pinning, GPU mask validation and first-assigned-device selection, separate requested/allocated/used counts, and public redaction. The stage records the requested count; execution evidence subsequently verified **two requested, two allocated, one used** on `NVIDIA H100 80GB HBM3`. The public details expose those counts without exposing private GPU masks or UUIDs.

The genuine push updated the existing `runs/1stp-smoke-check` branch to [`347623c02d64e440bc4ef61cbce787a95b923531`](https://github.com/azhang4216/dmasif-experiments/commit/347623c02d64e440bc4ef61cbce787a95b923531). It created new run `cebcd3be-6636-41c9-82d7-49ce1e61b38e` and job `114515`, submitted at **2026-10-02 16:32:05 UTC**. After waiting for priority, it started at **16:39:28 UTC** and ended at **16:41:09 UTC**, completing in **101 seconds** with exit `0:0`. The retained event sequence is `WAITING_FOR_CAPACITY → PREPARING → SUBMITTING → QUEUED → RUNNING → VALIDATING_RESULTS → SUCCEEDED`.

The new UUID and job output directory preserve the original branch's earlier failed run; the push did not reuse its output path. Read-only comparisons before and after execution verified unchanged file hashes and modification times for both failed runs' saved requests, staging/batch files, logs, execution evidence, and scientific source.

During the tunnel update, changing only the webhook URL removed its signing secret, and the initial delivery was rejected with HTTP 401 before run acceptance. The operator restored the complete webhook configuration with the existing protected secret and redelivered that same push; GitHub then received HTTP 202. No new experiment commit or duplicate scheduler submission was needed. Future endpoint updates must securely provide the full configuration and existing signing secret, then verify a signed delivery.

Both the local and public acceptance verifiers returned **passed**, checking the real result, logs, exact commit, scheduler outcome, timings, and source/runtime/checkpoint/adapter hashes. The downloaded `1STP.npz` contains **901 atoms, 3,928 points, and 16-dimensional features**, with validated array shapes, finite values, and atom mappings. Its size is **803,787 bytes** and its SHA-256 is `046befd88b2067c9a523b5cd13a3bf16e6ba550512c002cd781d851ecf4ecb62`. Local and public downloads match. The original invalid-config case was rechecked: `seed` remains the rejected field, with zero scheduler jobs and zero artifacts.

Actual Chrome desktop and mobile checks confirmed the [temporary public run page](https://firms-thirty-qualifying-folk.trycloudflare.com/runs/cebcd3be-6636-41c9-82d7-49ce1e61b38e), its successful status, timings, result information, and working download. The history retains all three earlier records. Earlier queue estimates were superseded by the actual start time; they were never treated as execution evidence. The site remains dependent on the laptop and tunnel. Further submissions are paused and the operator default is restored to one; the completed test retains its immutable count of two. Routine one-GPU H100/H200 access remains an operator question.

Read-only warning investigation traced startup shell warnings to the original container's CUDA/driver checks in `/etc/shinit_v2` and a memory-stat deprecation warning to the saved research code. Neither prevented this H100 extraction, but success does not establish that the warning-producing startup checks ran correctly. Optional cleanup belongs in a new immutable runtime or new research commit, with fresh validation; no blanket suppression or changes to the successful run were made. These follow-ups are in [TODO.md](../TODO.md).

## Additional checks passed

- Redelivered the genuine valid GitHub webhook: HTTP 202 with `duplicate: true`, the original run UUID, and still exactly one scheduler job.
- Stopped and restarted the application around a consistent backup: both records and the existing Slurm job remained intact; monitoring resumed without resubmission.
- Checked the actual local and public pages in Chrome at desktop and mobile widths: history, queued details, rejected details, exact code links, configuration, source/runtime/checkpoint/adapter hashes, real state history, and clear pending-result states.
- Checked public responses for the configured private host, username, key location, host-key file, and project root; none were exposed. Saved source archives and raw rejected YAML are not public downloads.
- Independent UI/privacy and documentation reviews found no remaining blocker before GPU execution.
- Application verification: 164 tests passed; Ruff, JavaScript syntax, and whitespace checks passed for the implementation revision above.

## Still required

1. Confirm a permitted routine one-GPU H100/H200 route or obtain a separate resource decision before enabling more submissions. The successful two-GPU reservation was authorized for one test only.
2. Select an always-on host and stable HTTPS address if continuous availability is required. The current public tunnel depends on the laptop staying awake and connected.
3. Add the other researchers' approved GitHub identities before they submit work.

The requested valid-run and invalid-config acceptance tests are complete. Blackwell support, warning cleanup, and full-training/resumption support remain separate follow-up work.

Each accepted run uses its own UUID and scheduler-job output directory, with retained submission/execution claims. The existing research checkout and shared historical output directory are not used as writable destinations. A repeat is a new commit and new run; webhook replay and application restart must reuse the existing identity.

See [the live submission guide](live-submissions.md) for operation and acceptance verification. Private configuration, SSH details, receipts, and raw diagnostic files are deliberately excluded from this report and from Git.
