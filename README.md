# cris-jobs-console — dashboard and infrastructure

> [!IMPORTANT]
> **Do not push experiment code, experiment configurations, or `runs/*` branches to this repository.**
>
> **To run an experiment, follow the [dmasif-experiments README](https://github.com/azhang4216/dmasif-experiments#readme).** Make your experiment changes and submit your runs in that repository.

This repository maintains the dashboard and the service that submits jobs to the cluster. The documentation below is for developing and operating that service. The public, read-only dashboard shows each run's researcher, exact code and configuration, progress, logs, and validated results.

The first release supports the existing dMaSIF **feature-extraction** interface. Full training and checkpoint resumption need a later adapter. The local demonstration uses synthetic inputs/features and never executes research code, SSH, or GPU jobs.

**Current free pilot:** the local app receives signed pushes from the private research repository and submits isolated Slurm jobs. A temporary HTTPS tunnel connects GitHub and shares the read-only dashboard. See [the live submission guide](docs/live-submissions.md). The separate [cluster history guide](docs/live-test.md) covers observation without submissions. GitHub Pages cannot run the backend; an always-on host is still needed for permanent deployment.

## Run the local demo

Requires Python 3.12+ and Git. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
dmasif-console demo --serve
```

Open **http://127.0.0.1:8000**; no login is required. Two signed sample pushes move through the queue and produce separate downloadable NPZ files. The page clearly labels the data as simulated. Stop with Ctrl+C.

Demo state and the generated webhook secret live in ignored `.state-demo/`. The command refuses to overwrite an existing demo; use a new `--state-dir .state-demo-2`, or restart the existing one:

```bash
export DMASIF_WEBHOOK_SECRET_FILE="$PWD/.state-demo/webhook-secret"
dmasif-console --config .state-demo/config.yaml serve --host 127.0.0.1 --port 8000
```

The demo needs the development dependencies because it exercises the real ASGI receiver using a local test client. Deployed web/worker services need only `requirements.lock`.

## What is implemented

| Component | Behavior |
| --- | --- |
| Signed webhook receiver | Raw-body HMAC, repository/actor/branch policy, durable acknowledgement, replay protection |
| Dashboard | History/details, filters, local timestamps, live logs, source links, validated downloads |
| SQLite store | Four tables, persistent state, atomic single-slot reservation, scheduler evidence and events |
| Source preparation | Exact SHA archive, saved configuration/policy, input manifest; rejects LFS/submodules/unsafe paths |
| Worker | Singleton lock, FIFO application queue, monitoring, bounded caching, restart recovery |
| SSH/Slurm adapter | Fixed remote protocol, strict host verification, claims/receipts, reconciliation, cancellation |
| Result validator | NPZ schema/shapes/finite values, checksums, atomic publication of validated bytes |
| Operator CLI | Pause/resume, reconcile/cancel, evidence-based resolution, consistent backup, restore with duplicate-delivery recovery |
| Single-service hosting | Supervised web + worker, persistent SQLite/files, health checks, rotating local backups |
| Local simulator | Persistent fake scheduler exercising the same run lifecycle without SSH or scientific execution |
| Live observer | Read-only SSH monitoring of existing Slurm jobs, cached locally, with unknown Git provenance kept explicit |

Every run writes beneath a new `runs/<run_uuid>/jobs/<slurm_job_id>/` directory. The generated template never uses the existing shared test-output directory. A persistent execution claim prevents the same run from executing twice; intentional repeats always get new run IDs.

Scheduler exit zero is insufficient for success: every expected output must validate. Lost submission responses keep capacity reserved and trigger reconciliation, never an automatic second `sbatch`.

The operator's `presets.quick-test.gpus` setting defaults to one and accepts only the integers one or two. Reserving two requires an explicit supported H100/H200 type; researchers cannot change it in their run YAML. The October 2 acceptance test reserved two H100 GPUs while extraction used only the first GPU assigned by Slurm. Requested, allocated, and used counts are recorded separately; allocating two does not enable multi-GPU extraction. See the [adapter contract](docs/cluster-adapter.md#match-the-runtime-to-the-gpu).

## Optional deployment on Render

Use **one paid Render web service with one persistent disk**. The service runs the website and job monitor together; SQLite stores history on the disk. Training/extraction stays on the lab's GPUs. There is no separate database, worker service, or GitHub Pages site.

Start with [the Render setup guide](docs/render.md). [render.yaml](render.yaml) starts with one 0.5-CPU/512-MB service and a 10-GB disk, about $9.50/month at October 2026 prices before taxes and usage extras. Review Render's displayed cost and check peak memory during the pilot. Daily application backups retain three copies on that disk; an operator must also export backups elsewhere. Automatic infrastructure deployments are disabled, so experiment pushes do not restart the application.

The submission service starts with real submissions disabled in operator configuration. Complete the approved repository/actor IDs, immutable input/image/checkpoint hashes, and private site settings from [config/example.yaml](config/example.yaml). Placeholder hashes deliberately fail validation. The Blueprint prompts for private values; supply them in Render, never in Git. A Render deployment has not yet been verified; the current live preview uses the laptop and a temporary tunnel.

The existing [Docker Compose guide](docs/deployment.md) remains an alternative for a lab-managed Linux server.

## Credential handling

- Real SSH keys, login notes, `.env` files, deployment secrets, and application state are ignored by Git. Private original notes remain in ignored `.local/` on this workstation.
- Publishable code/docs use example cluster identities. Site-specific login details belong only in ignored operator configuration, mounted as a runtime secret.
- The current pilot loads the SSH key, verified `known_hosts`, operator configuration, repository token, and webhook secret from protected local files. A hosted deployment uses private files or its secret manager, never committed secret values.
- Dashboard pages, run APIs, logs, and result downloads are public and require no login. A private GitHub repository protects its source; it does not make the dashboard private. Only signed pushes from approved GitHub IDs can create runs; visitors cannot submit or cancel jobs.
- The single application is one backend trust boundary: its administrator and backend processes can access its credentials. Browser clients cannot. The optional Compose setup provides separate web/worker secret mounts.
- Docker build contexts use an allowlist. Browser responses omit private policies/paths and redact current and historical cluster login identities from logs and errors.
- An optional private-repository token belongs in a backend secret file. GitHub Actions secrets are not automatically runtime secrets; never put cluster credentials into a Pages bundle or experiment workflow.

Keep application-host administration limited to operators who may access the cluster credential. Sharing one cluster Unix identity provides attribution and protection from accidental overwrites, not isolation from trusted code already running under that identity.

## Verify

```bash
pytest -q
ruff check dmasif_console cluster_adapter tests
node --check dmasif_console/static/console.js
```

Tests cover signed/replayed webhooks, identity spoofing, concurrent acceptance, secret redaction, source pinning, output collisions, corrupt/incomplete results, lost submission receipts, duplicate execution claims, cache recovery, and restore reconciliation. They use local temporary data and fake scheduler commands.

The live pilot has a frozen container, a versioned cluster adapter, and a signed webhook on the private research repository. Real pushes verified invalid-config rejection, submission, running-state observation, duplicate delivery, and restart recovery. The first two extraction attempts failed on incompatible Blackwell GPUs because the cluster redirects single-GPU H100 requests to that pool. The explicitly authorized October 2 test reserved two H100 GPUs, used one for extraction, and completed job `114515` in **101 seconds**, producing a validated `1STP.npz`. Further submissions remain paused and the operator default is restored to one; the two-GPU permission covered that test only. **H100/H200 with the existing runtime remains the target; Blackwell support is deferred in [TODO.md](TODO.md).** Permanent hosting and routine one-GPU allocation still need resolution. See the [acceptance record](docs/acceptance-2026-10-01.md) for verification evidence and [verification procedure](docs/live-submissions.md#acceptance-checks).

The design and review decisions are in [ARCHITECTURE.md](ARCHITECTURE.md).
