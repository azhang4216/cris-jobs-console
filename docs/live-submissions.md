# Test real submissions locally

The research repository is [azhang4216/dmasif-experiments](https://github.com/azhang4216/dmasif-experiments). Its `main` branch is for development; pushes to `runs/<experiment>` request a run. The dashboard and receiver run together on the application host. Researchers do not receive SSH credentials.

The operator registers immutable input, checkpoint, and container hashes, installs the fixed adapter in a new managed cluster root, and approves numeric GitHub user IDs. The extraction preset uses one registered `1STP.pdb` input, one H100 GPU, eight CPU cores, 64 GB memory, and a 15-minute limit. The H100 restriction keeps this frozen runtime off incompatible Blackwell GPUs. Runtime packaging is a separate CPU-only setup job.

## Start the configured workstation

Private configuration and state for this workstation are in ignored `.state-managed-live/`. These files are deliberately absent from Git; another host must receive its own protected configuration and secrets.

From the console checkout:

```bash
export DMASIF_WEBHOOK_SECRET_FILE="$PWD/.state-managed-live/webhook-secret"
export DMASIF_REPOSITORY_TOKEN_FILE="$PWD/.state-managed-live/repository-token"
export GIT_ASKPASS="$PWD/deploy/git-askpass.py"
export DMASIF_BACKUP_DIR="$PWD/.state-managed-backups"
.venv/bin/dmasif-console --config .state-managed-live/config.yaml serve --host 127.0.0.1 --port 8002
```

Open **http://127.0.0.1:8002**. One worker checks active jobs every 30 seconds. Browsers read cached state every 15 seconds; opening more tabs does not start more job workers.

Automatic backups retain three daily copies of the database and saved sources in a separate private directory. These copies are on the same laptop; export protected backups elsewhere for recovery from a lost computer.

Keep the laptop awake, connected, and running both the application and its tunnel. macOS `caffeinate -i` can prefix the serve command to prevent idle sleep; closing the laptop lid can still put it to sleep. Closing the application does not cancel a submitted Slurm job. Restarting with the same state directory resumes monitoring without submitting that job again.

## Connect GitHub to the local receiver

Run this in another terminal:

```bash
cloudflared tunnel --url http://127.0.0.1:8002 --no-autoupdate
```

Configure a push-only webhook in the research repository with the tunnel's HTTPS URL plus `/webhooks/github`, JSON content type, SSL verification enabled, and the exact private webhook secret. Keep the secret out of screenshots, repository files, and command arguments. A valid signature, approved repository ID, approved personal GitHub user ID, and matching branch are all required.

Check the webhook delivery response before pushing a test branch. If the tunnel restarts, update the existing webhook URL; do not create additional hooks. Deliveries missed while the laptop was offline must be inspected and redelivered from GitHub. A redelivery must retain the original delivery identity, and the service will reuse its existing run.

A Quick Tunnel provides a temporary public test URL. It is not an always-on deployment, has no uptime guarantee, and its URL changes on restart. A stable shared service needs an always-on host and a stable HTTPS address. GitHub Pages alone cannot run this backend. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/), [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

## Acceptance checks

Use two distinct branches and retain their complete commit SHAs. Do not change a run's saved files or manually force its state to make a test pass.

| Case | Request | Evidence required |
| --- | --- | --- |
| Valid extraction | `runs/1stp-smoke-check-h100`, registered dataset, integer seed | Genuine GitHub delivery; exact actor/commit/config; recorded queue and running observations; terminal Slurm success; validated NPZ; readable logs; working checksum-verified download. |
| Invalid configuration | `runs/config-rejection-check`, seed set to a string | Genuine GitHub delivery; rejected status; clear seed type/range message; exact actor/commit/source identity; no Slurm job, GPU use, or result files. |

The detail page retains received/submitted/started/ended times, actual state transitions, GitHub identity, code link, configuration, runtime/checkpoint/source hashes, scheduler ID and exit code, logs, and result validation. Raw rejected YAML remains private; the site displays safe field-specific diagnostics. A very short job can finish between polls; the service must not invent a running observation. Scientific success requires valid results as well as a successful scheduler exit.

Download the real NPZ and validate its hash, nonempty point/atom arrays, finite numeric values, 16-dimensional features, and metadata. Verify the same information locally and through the public URL, including mobile layout. Redeliver an accepted webhook and check that it creates neither a second run nor a second scheduler job.

With the project installed in `.venv`, run the saved verifier from this checkout:

```bash
.venv/bin/python scripts/verify_live_acceptance.py http://127.0.0.1:8002
```

It defaults to the H100 retry's exact commit/repeat label, the original invalid-config commit, their branches, research repository, and `azhang4216` identity. The original unrestricted smoke test remains a failed historical run; it is not rewritten or counted as a pass. Use `--help` to override these identities, replace the base URL to verify the public site, or add `--output /tmp/dmasif-acceptance.json` to save its JSON summary. It uses only cached HTTP GET routes and checks real execution evidence plus the downloaded NPZ. Exit `0` means all checks passed, `2` means pending or unavailable, and `1` means verification failed. A queued job must remain pending; it is never counted as a pass.

Each run has a distinct UUID and allocation directory. Retain submission/execution claims and receipts; an uncertain response must be reconciled, never retried blindly. Do not reuse the historical shared output directory.

## Pause new cluster submissions

Pause cluster scheduling while continuing to monitor active work. Signed pushes are still accepted and their source/configuration is prepared; valid requests wait until scheduling resumes:

```bash
.venv/bin/dmasif-console --config .state-managed-live/config.yaml pause \
  --actor YOUR_GITHUB_LOGIN --reason "Operator maintenance"
```

Resume only when the configuration and runtime are ready:

```bash
.venv/bin/dmasif-console --config .state-managed-live/config.yaml resume \
  --actor YOUR_GITHUB_LOGIN --reason "Ready for registered extraction runs"
```

Keep the SQLite database, saved sources, and private operator configuration when moving to an always-on host. Stop the old worker before starting the new one; two application hosts must never submit independently from separate copies of the same state. Use [the backup/restore procedure](deployment.md#backups-restore-and-updates) for a move rather than copying a live SQLite file.
