# dMaSIF research console

A small GitHub-to-Slurm job service for a research lab. Researchers push experiment branches; a private, read-only dashboard shows who submitted each run, its exact code and configuration, progress, logs, and validated results. The backend holds the cluster credential. Researchers do not need cluster logins or individual website accounts.

The first release supports the existing dMaSIF **feature-extraction** interface. Full training and checkpoint resumption need a later adapter. The local demonstration uses synthetic inputs/features and never executes research code, SSH, or GPU jobs.

## Run the local demo

Requires Python 3.12+ and Git. From this directory:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.lock
python -m pip install --no-deps -e .
dmasif-console demo --serve
```

Open **http://127.0.0.1:8000**. The command prints the local demo's shared viewer username/password. Two signed sample pushes move through the queue and produce separate downloadable NPZ files. The page clearly labels the data as simulated. Stop with Ctrl+C.

Demo state and generated secrets live in ignored `.state-demo/`. The command refuses to overwrite an existing demo; use a new `--state-dir .state-demo-2`, or reuse the existing one in two terminals:

```bash
export DMASIF_WEBHOOK_SECRET_FILE="$PWD/.state-demo/webhook-secret"
export DMASIF_VIEWER_PASSWORD_FILE="$PWD/.state-demo/viewer-password"
dmasif-console --config .state-demo/config.yaml worker
# In a second terminal, activate the same environment and set the same variables:
dmasif-console --config .state-demo/config.yaml web
```

The demo needs the development dependencies because it exercises the real ASGI receiver using a local test client. Deployed web/worker services need only `requirements.lock`.

## Researcher workflow

In the **research repository**, copy [config/run.example.yaml](config/run.example.yaml) to `experiments/run.yaml`, commit the desired code, and push a branch named `runs/YOUR_GITHUB_LOGIN/experiment-name`.

- Each qualifying push creates one run for the final full commit SHA.
- GitHub's verified pushing account identifies the researcher; commit authorship is recorded separately.
- A repeated webhook returns the existing run. A new configuration commit changing `repeat_id` creates a separate run.
- All job outcomes appear on the dashboard, including rejected configuration and missing outputs.

See [the researcher guide](docs/researcher.md) for the first-run steps and recovery help.

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

Every run writes beneath a new `runs/<run_uuid>/jobs/<slurm_job_id>/` directory. The generated template never uses the existing shared test-output directory. A persistent execution claim prevents the same run from executing twice; intentional repeats always get new run IDs.

Scheduler exit zero is insufficient for success: every expected output must validate. Lost submission responses keep capacity reserved and trigger reconciliation, never an automatic second `sbatch`.

## Deploy on Render

Use **one paid Render web service with one persistent disk**. The service runs the website and job monitor together; SQLite stores history on the disk. Training/extraction stays on the lab's GPUs. There is no separate database, worker service, or GitHub Pages site.

Start with [the Render setup guide](docs/render.md). [render.yaml](render.yaml) defines one 1-CPU/2-GB service and a 10-GB disk; review Render's displayed cost before creating it. Daily application backups retain three copies on that disk; an operator must also export backups elsewhere. Automatic infrastructure deployments are disabled, so experiment pushes do not restart the application.

The service starts with real submissions disabled in operator configuration. Complete the approved repository/actor IDs, immutable input/image/checkpoint hashes, and private site settings from [config/example.yaml](config/example.yaml). Placeholder hashes deliberately fail validation. No cloud resources have been created and no cluster changes have been made.

The existing [Docker Compose guide](docs/deployment.md) remains an alternative for a lab-managed Linux server.

## Credential handling

- Real SSH keys, login notes, `.env` files, deployment secrets, and application state are ignored by Git. Private original notes remain in ignored `.local/` on this workstation.
- Publishable code/docs use example cluster identities. Site-specific login details belong only in ignored operator configuration, mounted as a runtime secret.
- Render stores the SSH key, verified `known_hosts`, and private operator configuration as secret files. Webhook/viewer secrets are entered in Render, never in `render.yaml`.
- The single Render service is one backend trust boundary: its administrator and backend processes can access its credentials. Browser clients cannot. The optional Compose setup provides separate web/worker secret mounts.
- Docker build contexts use an allowlist. Browser responses omit private policies/paths and redact current and historical cluster login identities from logs and errors.
- An optional private-repository token belongs in a backend secret file. GitHub Actions secrets are not automatically runtime secrets; never put cluster credentials into a Pages bundle or experiment workflow.

Keep Render administration limited to operators who may access the cluster credential. Sharing one cluster Unix identity provides attribution and protection from accidental overwrites, not isolation from trusted code already running under that identity.

## Verify

```bash
pytest -q
ruff check dmasif_console cluster_adapter tests
node --check dmasif_console/static/console.js
```

Tests cover signed/replayed webhooks, identity spoofing, concurrent acceptance, secret redaction, source pinning, output collisions, corrupt/incomplete results, lost submission receipts, duplicate execution claims, cache recovery, and restore reconciliation. They use local temporary data and fake scheduler commands.

The implementation has been exercised locally. Real Apptainer/Slurm execution, the chosen frozen runtime, site-specific mounts, and GPU performance still require an authorized cluster smoke test. No remote changes or GPU jobs were made during implementation.

The design and review decisions are in [ARCHITECTURE.md](ARCHITECTURE.md).
