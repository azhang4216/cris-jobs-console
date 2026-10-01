# Alternative: deploy on a lab-managed Linux host

The current free test uses [the laptop and a temporary tunnel](live-test.md). Use this guide if the lab chooses to maintain its own Linux server; [one Render application](render.md) remains another optional deployment. Compose keeps the web and worker in separate containers; the Render deployment runs both inside one service.

Use one always-on Linux machine with Docker Compose. Caddy provides HTTPS, the web process receives signed pushes and serves the dashboard, and one worker communicates with the scheduler. The application host needs no GPU. SQLite, saved source, and the artifact cache stay on a persistent local Docker volume.

These are deployment instructions, not a record of deployment. Building or reading this repository does not install anything on the cluster. The supplied policy starts with submissions disabled; enabling real jobs and installing the cluster adapter are later operator actions.

## Why this includes a server

GitHub Pages hosts static HTML, CSS, and JavaScript. It cannot run this Python webhook receiver, durable worker, SQLite database, or SSH client. The simplest setup serves the website and backend together at one HTTPS address. [GitHub Pages documentation](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

A Pages frontend would still need this server, plus a separate static frontend and CORS configuration for its public API requests. That split deployment is not included in v1. It would not remove the server or simplify the four-person workflow.

GitHub Actions secrets exist inside authorized workflow jobs. They do not automatically become secrets on the application host. Never embed the SSH key, webhook secret, or repository token into a Pages bundle, browser JavaScript, or research workflow. Researchers only need the website URL and their personal GitHub account. Dashboard pages, run APIs, logs, and result downloads are public; a private GitHub repository protects its source, not the website.

## Files and responsibilities

| File | Purpose |
| --- | --- |
| `deploy/Dockerfile` | CPU-only application image with Python, Git, and OpenSSH |
| `deploy/Dockerfile.dockerignore` | Build-context allowlist; excludes root text files, secrets, config, and local state |
| `deploy/compose.yaml` | One web process, one worker, Caddy, persistent volumes |
| `deploy/compose.private-repo.yaml` | Optional worker-only token for private GitHub source |
| `deploy/Caddyfile` | HTTPS reverse proxy; only ports 80 and 443 are public |
| `deploy/.env.example` | Non-secret host/domain/file-location settings |
| `deploy/private/` | Ignored operator config and mounted secret files |

Docker's Dockerfile-specific ignore file takes precedence over any root `.dockerignore`; this deployment does not send unrelated repository files to the image builder. [Docker build context](https://docs.docker.com/build/concepts/context/)

## Prepare launch inputs

The operator supplies:

- An always-on Linux host, current Docker Engine and Compose, and outbound HTTPS/SSH access.
- A DNS name pointing to that host and inbound TCP ports 80/443. Do not publish port 8000.
- The approved GitHub repository's numeric ID, exact name/URL, four allowed numeric actor IDs, and a repository webhook.
- A named operator contact and a random webhook secret.
- The approved unattended SSH credential and an independently verified `known_hosts` entry.
- The installed fixed cluster adapter, approved scheduler account/preset, and immutable demo input manifest.
- A tested frozen Apptainer image and checkpoint, each with its verified SHA-256 checksum.

The adapter/runtime/input registration must be completed before enabling SSH-mode work. Use the actual tested runtime rather than assuming an existing SIF matches a patched sandbox. Confirm the account can be used for this delegated submission workflow. No SSH credentials are given to researchers.

The fixed adapter installation, runtime contract, and first GPU validation procedure are in [cluster-adapter.md](cluster-adapter.md).

For public DNS, Caddy provisions and renews HTTPS certificates when the domain points to the host, ports are reachable, and its data directory is persistent. Incorrect AAAA records can break issuance even when the A record is correct. [Caddy automatic HTTPS](https://caddyserver.com/docs/automatic-https)

## Prepare local deployment configuration

Run these steps on the application host from the infrastructure repository root. Keep research code in its separate research repository.

```bash
mkdir -p deploy/private deploy/backups
chmod 700 deploy/private deploy/backups
cp deploy/.env.example deploy/.env
cp config/example.yaml deploy/private/operator.yaml
chmod 600 deploy/.env deploy/private/operator.yaml
```

Edit `deploy/.env` with the public domain and operator email. Its file locations are relative to `deploy/`, the first Compose file's directory. It contains locations, not credential contents. Pin tested Python/Caddy image digests for the actual release if reproducible image selection is required; the defaults are version-family tags.

Edit `deploy/private/operator.yaml` with the approved site values. Leave `submissions_enabled: false`. Set `mode: ssh` only when its runtime/checkpoint hashes and operator contact are complete. The container overrides `state_dir` to `/var/lib/dmasif-console`; do not map it to NFS. Do not put the webhook secret, SSH key, or repository token in YAML.

Generate the webhook secret without printing it:

```bash
python3 - <<'PY'
import os
from pathlib import Path
import secrets

path = Path("deploy/private/webhook-secret")
descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w") as stream:
    stream.write(secrets.token_urlsafe(40) + "\n")
PY
```

Install the approved private key at `deploy/private/cluster-key`, and the verified host entry at `deploy/private/known-hosts`. Keep the key in valid OpenSSH/PEM format; its extension is irrelevant. Unattended batch SSH cannot answer an interactive passphrase prompt. Provision the intended service credential with the cluster operator.

The containers run as UID/GID **10001:10001**. On a standard rootful Linux Docker installation, prepare file ownership so that user can read the bind-mounted secrets:

```bash
sudo chown 10001:10001 deploy/private/operator.yaml \
  deploy/private/webhook-secret \
  deploy/private/cluster-key deploy/private/known-hosts deploy/backups
sudo chmod 600 deploy/private/operator.yaml \
  deploy/private/webhook-secret \
  deploy/private/cluster-key deploy/private/known-hosts
sudo chmod 700 deploy/backups
```

Use an operator editor with appropriate permissions for later edits. Do not make the private key world-readable to fix a permissions error. Rootless/user-namespace Docker needs the corresponding host UID mapping instead of assuming 10001 maps directly.

Compose mounts secrets as files and grants them per service. With local file-backed secrets, `uid`/`gid`/`mode` remapping is not implemented; preserve the correct host ownership/mode. The web container mounts only operator configuration and the webhook secret. The worker mounts operator configuration, the SSH key, and known hosts. Neither Caddy nor the web container receives the SSH key. [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/), [service secret options](https://docs.docker.com/reference/compose-file/services/#secrets)

## Private research repositories

Skip this section for a public repository. For a private repository, put a GitHub token restricted to reading the approved repository's contents in `deploy/private/repository-token`, owned by 10001 and mode 600. Set `REPOSITORY_TOKEN_FILE` if using a different file location.

Add `-f deploy/compose.private-repo.yaml` to every Compose invocation. Its fixed Git credential helper reads the token file only in the worker. It never embeds the token in a clone URL or persists it into an experiment snapshot.

The following shell function keeps command options consistent. Include the commented second `-f` on the same Docker Compose invocation when private-repository access is needed:

```bash
dc() {
  docker compose --env-file deploy/.env -f deploy/compose.yaml "$@"
}
# Private repository alternative:
# dc() {
#   docker compose --env-file deploy/.env -f deploy/compose.yaml \
#     -f deploy/compose.private-repo.yaml "$@"
# }
```

## Start with submissions paused

After the host, credentials, adapter, and DNS are ready for an authorized deployment:

```bash
dc config --quiet
dc build
dc up -d
dc ps
```

The initial named state volume gets UID 10001 ownership from the image. Both application services require writes for SQLite WAL; never mount that volume read-only or share it across application hosts. Do not scale the worker. Its process-lifetime file lock rejects a second worker against the same state volume.

Open the HTTPS address without signing in. The public dashboard is read-only; signed GitHub pushes and the approved numeric sender IDs identify and authorize submitters. Web/API requests read local saved state and never perform SSH.

Configure a repository webhook with:

- Payload URL: `https://YOUR_DOMAIN/webhooks/github`.
- Content type: `application/json`.
- Secret: the exact contents of the separate webhook-secret file.
- Events: pushes only; SSL verification enabled.

Keep the secret out of chat, command history, and the repository. The receiver validates the raw-body HMAC and actor/repository policy. Public viewing does not bypass these submission checks. [GitHub signature validation](https://docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries)

Check the delivery response in GitHub and the received-event activity on the dashboard. Initially do not push a run branch unless a queued test run is intended. Jobs remain paused until both the policy and stored pause state allow submissions.

For the first authorized demo, set `submissions_enabled: true`, recreate web and worker to reload configuration, and clear the stored pause with an attributed reason:

```bash
dc up -d --force-recreate web worker
dc run --rm worker resume --actor YOUR_OPERATOR_NAME --reason "Approved one-dataset launch"
```

Run the demo twice and as two allowed researchers. Verify unique directories, attribution, pinned source/config, valid artifacts, and no changes to the old shared output location before extending datasets.

## Operator commands

Browser users cannot cancel, resubmit, or mutate state. A trusted host operator uses the local CLI. Replace the sample actor, run UUID, and reason with meaningful values.

Pause or resume new submissions without stopping monitoring:

```bash
dc run --rm worker pause --actor YOUR_OPERATOR_NAME --reason "Investigating a run"
dc run --rm worker resume --actor YOUR_OPERATOR_NAME --reason "Investigation complete"
```

Pause takes effect immediately in SQLite; `resume` still requires configuration `submissions_enabled: true`. A possibly live ambiguous run continues to hold capacity.

Commands that inspect/cancel cluster work need exclusive worker ownership. Stop the worker, perform the action, then restart it even if the action reports an unresolved condition:

```bash
dc stop worker
dc run --rm worker reconcile RUN_UUID --actor YOUR_OPERATOR_NAME --reason "Inspect lost submission response"
dc start worker
```

Use `cancel RUN_UUID` in place of `reconcile RUN_UUID` for an intended cancellation. It verifies known job mappings and waits for subsequent terminal evidence; a cancellation request is not an immediate successful outcome. Never delete submission/execution claims, blindly repeat `sbatch`, or manually release a possibly live reservation. A resolved failed run is repeated through a new Git commit.

If a submission claim remains but receipts/accounting cannot explain whether the helper reached `sbatch`, first obtain external confirmation from the site/operator that the old helper cannot still submit and no associated job remains live. A missing job in a queue listing alone is insufficient. Then, with the worker stopped, close the uncertain run using an audited resolution:

```bash
dc stop worker
dc run --rm worker resolve RUN_UUID --actor YOUR_OPERATOR_NAME \
  --reason "Site investigation confirmed this run cannot execute" \
  --evidence "Site ticket/reference documenting helper termination and job verification" \
  --confirm-no-live-job
dc start worker
```

`resolve` accepts only submission-unknown/needs-review runs, requires working scheduler observation, and refuses known jobs without terminal evidence. It records the verification privately, releases the slot, and marks the run failed. It never removes claims, invokes `sbatch`, or declares scientific success. Use a new commit for an intentional repeat. A restore rechecks the scheduler before accepting a previously recorded resolution.

Check operational logs with `dc logs --tail=100 web worker`. Treat host logs, database backups, and state files as private operator material. The dashboard's public serialization hides internal connection details, but host-side evidence contains operating paths and identity data.

## Backups, restore, and updates

Back up outside the live state volume. Stop the worker because backup takes the singleton lock. The CLI uses SQLite's backup API and retains source archives; it does not copy credentials or guarantee preservation of the disposable local artifact cache.

```bash
dc stop worker
dc run --rm -v "$PWD/deploy/backups:/backups" worker backup /backups/backup-YYYYMMDD
dc start worker
```

Keep operator configuration, frozen release hashes, and protected secret recovery separately. Retain cluster run directories and monitor both host/cluster free space. Cache eviction never deletes remote results.

Restore into a **new** state volume; do not overwrite active history:

1. Stop web and worker. Set `submissions_enabled: false`, and change `DMASIF_STATE_VOLUME` in `deploy/.env` to a new name. Preserve the old volume.
2. Run restore with the backup mounted read-only:

   ```bash
   dc run --rm -v "$PWD/deploy/backups:/backups:ro" worker restore \
     --backup /backups/backup-YYYYMMDD --actor YOUR_OPERATOR_NAME --reason "Host recovery"
   ```

3. Restore inventories remote requests/receipts, rebuilds deduplication mappings, and reconciles potentially submitted jobs. It performs cluster reads and therefore needs approved working SSH access. If unresolved evidence remains, it fails closed; use `restore` again without `--backup` after resolving the evidence.
4. Start web and worker, inspect recovered history, and only then explicitly enable submissions and resume. Never infer that an older database means an old push was never submitted. Webhooks fail closed while restore evidence is unresolved.

GitHub does not automatically redeliver a failed webhook. After any receiver downtime, inspect delivery history and request redelivery where necessary; deduplication handles repeats. [GitHub failed deliveries](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)

For an application upgrade, pause submissions, make a consistent backup, build the intended reviewed infrastructure revision, and recreate the services. Keep the prior image and state backup for operator recovery. Do not run `docker compose down --volumes` on a live installation: that removes persistent state.

## Deployment status

The repository provides the application and deployment scaffolding. Actual host provisioning, verified host key, runtime freezing, cluster-adapter installation, secret mounting, and an authorized GPU smoke test are site-specific launch work. No deployment or cluster mutation is performed by this document.
