# Real submission acceptance record — October 1, 2026

**Status: incomplete.** The invalid configuration case passed. The valid case reached Slurm's queue and is waiting for allocation; GPU execution and scientific results have not yet passed acceptance. This record describes genuine GitHub pushes and cluster observations, not the synthetic demo.

## Setup tested

- Infrastructure: [dmasif-console, `02d5b99`](https://github.com/azhang4216/dmasif-console/commit/02d5b99).
- Private research repository: [dmasif-experiments](https://github.com/azhang4216/dmasif-experiments), based on `cszcz00/dMaSIF` `modern-stack` commit `a56a69f16ea97dd3bb59d91cdcf9a298c412ce90`.
- One supervised local application and worker, SQLite, a signed GitHub push webhook, and a temporary public HTTPS tunnel.
- One immutable `1STP` input, registered checkpoint, frozen Apptainer image, and installed adapter release `v1-7f73a00d9461`.
- One GPU, eight CPUs, 64 GB memory, 15-minute limit. The runtime's CPU launch passed; the extraction allocation is pending.
- Approved GitHub sender: `azhang4216`, numeric ID `64555467`. No other researchers are authorized yet.

## The two real pushes

| Case | Branch and exact commit | Run | Observed outcome |
| --- | --- | --- | --- |
| Valid extraction | `runs/1stp-smoke-check`, [`74f070320047e0b6f00cedeb1737c3ebdcb075ee`](https://github.com/azhang4216/dmasif-experiments/commit/74f070320047e0b6f00cedeb1737c3ebdcb075ee) | `641eb5c0-2770-45ce-b4dc-58b1fa0bdc89` | Received → waiting for capacity → preparation/submission → queued. Slurm job `111423`, reason `Priority`; no running or completed observation yet. |
| Invalid configuration | `runs/config-rejection-check`, [`7a8ac87600b5fbf089bb77d7dc3e00bbc4d452a6`](https://github.com/azhang4216/dmasif-experiments/commit/7a8ac87600b5fbf089bb77d7dc3e00bbc4d452a6) | `7388ff52-0210-4904-9ab9-a235108ab2c9` | Rejected with a field-specific error; zero scheduler jobs and zero artifacts. |

Both GitHub deliveries received HTTP 202. The valid job was submitted at **2026-10-01 01:59:30 UTC**. The invalid request used a string instead of an integer for `seed`; the site reports:

> Invalid experiments/run.yaml. experiments/run.yaml.seed: Must be an integer from 0 to 4294967295 (without quotes). No cluster job was submitted.

The rejected request still retains its exact actor, branch, commit link, source checksum, and validation report. It does not consume a GPU allocation.

## Additional checks passed

- Redelivered the genuine valid GitHub webhook: HTTP 202 with `duplicate: true`, the original run UUID, and still exactly one scheduler job.
- Stopped and restarted the application around a consistent backup: both records and the existing Slurm job remained intact; monitoring resumed without resubmission.
- Checked the actual local and public pages in Chrome at desktop and mobile widths: history, queued details, rejected details, exact code links, configuration, source/runtime/checkpoint/adapter hashes, real state history, and clear pending-result states.
- Checked public responses for the configured private host, username, key location, host-key file, and project root; none were exposed. Saved source archives and raw rejected YAML are not public downloads.
- Independent UI/privacy and documentation reviews found no remaining blocker before GPU execution.
- Application verification: 164 tests passed; Ruff, JavaScript syntax, and whitespace checks passed for the implementation revision above.

## Still required

1. Observe the valid job running and finishing successfully. Slurm has not provided a start estimate; no state may be fabricated to satisfy this check.
2. Verify scheduler exit code, actual GPU/runtime/source provenance, meaningful start/end times, readable logs, and one validated result.
3. Download the real NPZ locally and through the public site; verify its checksum, nonempty point/atom arrays, finite values, and 16-dimensional features.
4. Check the actual successful detail page and download in the browser.
5. Select an always-on host and stable HTTPS address if continuous availability is required. The current public tunnel depends on the laptop staying awake and connected.
6. Add the other researchers' approved GitHub identities before they submit work.

Each accepted run uses its own UUID and scheduler-job output directory, with retained submission/execution claims. The existing research checkout and shared historical output directory are not used as writable destinations. A repeat is a new commit and new run; webhook replay and application restart must reuse the existing identity.

See [the live submission guide](live-submissions.md) for operation and acceptance verification. Private configuration, SSH details, receipts, and raw diagnostic files are deliberately excluded from this report and from Git.
