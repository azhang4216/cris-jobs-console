# Deploy one Render application

This is an optional paid deployment. The current free live test uses the laptop and a temporary tunnel; see [live-test.md](live-test.md).

If deployed on Render, use one web service and one persistent disk. The service hosts the website, watches the job queue, and keeps SQLite history and small downloadable results on the disk. The cluster still runs dMaSIF and retains large outputs.

Researchers only need the dashboard URL and their personal GitHub account. The website is public and needs no login. A push to `runs/<experiment>`, such as `runs/pocket-v1`, records the exact commit and creates a new run with its own output directory. The signed webhook supplies the pushing researcher's login and numeric ID automatically; branch names do not identify their owner. There is no website submission form or researcher account.

## Set up once

1. **Connect the infrastructure repository**, `azhang4216/dmasif-console`, to Render. Keep its `main` branch protected and separate from the dMaSIF research repository. Before publishing any changes, check Git's file list; private configuration, keys, notes, and state must stay ignored.
2. **Prepare the private operator configuration.** Copy [config/example.yaml](../config/example.yaml) to ignored `deploy/private/operator.yaml`. Fill in the approved research repository and numeric researcher IDs, cluster settings, operator contact, dataset checksums, and tested runtime/checkpoint hashes. Keep `submissions_enabled: false`. The fixed cluster adapter and runtime must be installed and tested before enabling jobs; see [cluster setup](cluster-adapter.md). These instructions do not install anything remotely.
3. **Create a Render Blueprint** from the infrastructure repository. [render.yaml](../render.yaml) requests a paid `1c-2g` web service in Virginia, one instance, and a 10-GB disk. Review the displayed recurring charge before creating resources. Supply a long random value when Render prompts for `DMASIF_WEBHOOK_SECRET`.
4. **Add the three private files** below under the service's **Environment → Secret Files**. Do not paste them into GitHub, the Blueprint, application logs, or chat. The first deploy can fail while these files are missing; after saving all three, deploy the service again.
5. **Open the Render HTTPS URL**; no login is required. Configure the research repository's push webhook using the table below. Keep submissions disabled until the authorized cluster smoke test is ready.

| Render secret filename | Contents | Application location |
| --- | --- | --- |
| `operator.yaml` | Completed private operator configuration | `/etc/secrets/operator.yaml` |
| `cluster-key` | Approved unattended SSH private key, with its original multiline formatting | `/etc/secrets/cluster-key` |
| `known-hosts` | Independently verified cluster SSH host entry | `/etc/secrets/known-hosts` |

The key's extension does not matter. The key must be valid and usable without an interactive passphrase prompt. Each SSH call uses a temporary owner-readable copy of the mounted key, which is removed afterward; never make a private key public to fix SSH permissions.

| GitHub webhook setting | Value |
| --- | --- |
| Payload URL | `https://YOUR_SERVICE.onrender.com/webhooks/github` |
| Content type | `application/json` |
| Secret | The same value as Render's `DMASIF_WEBHOOK_SECRET` |
| Events | Push events only |
| SSL verification | Enabled |

Confirm that the cluster allows incoming SSH from the service's [Render outbound IP ranges](https://render.com/docs/outbound-ip-addresses). If it requires an institutional VPN or restricted source addresses, the site operator must arrange access first. No real connection has been verified from Render yet.

For the first authorized run, set `submissions_enabled: true` in Render's `operator.yaml` secret file and redeploy. In **Render → Shell**, clear any stored pause:

```bash
dmasif-console resume --actor YOUR_OPERATOR_NAME --reason "Approved initial extraction smoke test"
```

Follow [the researcher guide](researcher.md). Verify two separate run directories and validated outputs before expanding beyond the demo dataset. The current adapter supports feature extraction; full training requires a later adapter.

## What Render runs

The Dockerfile starts `dmasif-console serve`: a supervisor starts the web process and one worker, forwards shutdown, and exits if either unexpectedly stops. Render can then restart the service. `/healthz` checks local storage and that the supervised processes are running; the dashboard's last-check time shows whether cluster monitoring is current. A deployment interrupts the dashboard briefly; already submitted cluster jobs keep running, and the restarted worker reconciles their recorded identities.

| Location | Contents |
| --- | --- |
| `/var/data/state/console.sqlite3` | Job history, queue, delivery deduplication, operator events |
| `/var/data/state/sources/` | Saved source snapshots |
| `/var/data/state/artifacts/` | Bounded cache of validated downloads |
| `/var/data/backups/` | Rotating application backups |
| `/etc/secrets/` | Render-provided private files; excluded from application backups |

Keep one instance and the disk mounted at `/var/data`. Only this mount persists. Run operator commands in the live service's **Shell**, not a Render one-off job or pre-deploy command, because those do not have the disk. See [Render's disk requirements](https://render.com/docs/disks).

`render.yaml` disables automatic application deployment and previews are not enabled. Research pushes only reach the signed webhook; they cannot deploy infrastructure. Operators manually deploy reviewed `main` revisions. Render supplies the HTTPS endpoint, so there is no separate proxy, DNS setup, or Pages deployment required.

## Private research repository

For a private dMaSIF repository, create a GitHub token restricted to reading that repository's contents. Add it as a Render secret file named `repository-token`, then set these service environment variables:

```text
DMASIF_REPOSITORY_TOKEN_FILE=/etc/secrets/repository-token
GIT_ASKPASS=/usr/local/bin/dmasif-git-askpass
GIT_TERMINAL_PROMPT=0
```

The fixed credential helper reads the file for Git authentication. Do not put the token in the clone URL. Render's access to build the infrastructure repository does not automatically grant the running application access to the separate research repository.

## Secrets and access

Dashboard pages, run APIs, logs, and result downloads are public. Keeping either GitHub repository private protects that repository's source, not the website. A commit link still requires repository access to view the code on GitHub. Visitors cannot submit, cancel, or retry runs; the receiver requires a valid GitHub signature and an approved repository and numeric sender ID.

The browser never receives the SSH key, private operator configuration, or repository token. Render administrators and the backend can access the service's secrets, so only trusted operators should administer it. The web and worker share one service and operating-system user; this design does not isolate the key from a compromised web process.

Render makes [secret files](https://render.com/docs/configure-environment-variables#secret-files) available under `/etc/secrets`. It also places copies in the Docker build context. The Dockerfile-specific allowlist excludes them, and Dockerfile instructions never reference credential build arguments. Preserve these rules when editing the image; never replace explicit `COPY` instructions with an unrestricted `COPY . .`. See [Render's Docker secret handling](https://render.com/docs/docker).

Local keys, login notes, `.env` files, `deploy/private/`, `.local/`, and application state remain Git-ignored. This prevents accidental future additions; it cannot remove a secret already published in Git history. Application backups contain private metadata and source code, so treat those as private too.

## Daily operation

Pause new submissions while leaving monitoring active:

```bash
dmasif-console pause --actor YOUR_OPERATOR_NAME --reason "Investigating a run"
dmasif-console resume --actor YOUR_OPERATOR_NAME --reason "Investigation complete"
```

For exclusive operator work such as `reconcile`, `cancel`, `resolve`, or a manual `backup`, set `DMASIF_MAINTENANCE=1` in Render and select **Save and deploy**. Wait for that deploy to finish. The worker and automatic backups stop; the website and webhook return a maintenance response, while `/healthz` remains available. In the live service's Shell, run the needed command. Remove the maintenance variable and redeploy when finished. This is the application's maintenance setting, not Render's separate traffic-blocking feature.

Examples while application maintenance is active:

```bash
dmasif-console reconcile RUN_UUID --actor YOUR_OPERATOR_NAME --reason "Inspect lost submission response"
dmasif-console cancel RUN_UUID --actor YOUR_OPERATOR_NAME --reason "Researcher requested cancellation"
dmasif-console backup /var/data/backups/manual-YYYYMMDD-HHMM
```

A cancellation request is not proof that a job stopped. Never clear execution claims or repeat `sbatch` to address an uncertain run. The `resolve` command requires explicit site evidence that no helper can submit and no associated job remains live; see the [operator recovery explanation](deployment.md#operator-commands) for its conditions. Use the same `dmasif-console resolve ...` command in Render Shell with the worker stopped by maintenance.

## Backups and recovery

The service creates consistent application backups every 24 hours and keeps the latest three automatic copies. Each contains SQLite and retained source archives; disposable cached downloads and credentials are excluded. Settings are `DMASIF_BACKUP_DIR`, `DMASIF_BACKUP_INTERVAL_SECONDS`, and `DMASIF_BACKUP_KEEP`. Watch disk usage and backup errors in Render; retained source history can require a larger disk as the lab accumulates runs.

**These copies are on the same disk as the live database.** They help with application mistakes but cannot protect against loss of that disk or service. An operator must periodically export a completed backup directory to approved private storage using [Render's file transfer procedure](https://render.com/docs/disks#transferring-files). Off-service export is not automated in this version. Keep private operator configuration, secrets, and frozen cluster releases recoverable separately.

Use the application's SQLite-aware backups for recovery. Do not treat a live filesystem copy or a Render disk snapshot as the application restore procedure.

To restore:

1. Set `DMASIF_MAINTENANCE=1` and change the private operator config to `submissions_enabled: false`; deploy and wait for maintenance to be active. On a replacement service, also keep its webhook disconnected until restore finishes.
2. Transfer the selected complete backup directory to `/var/data/backups/` if it is not already there. In Render Shell, move the old `/var/data/state` directory to a new, unused name such as `/var/data/state-before-YYYYMMDD-HHMM`. Preserve it. Do not overwrite existing state or rename the disk mount itself.
3. Restore into the fresh state location:

   ```bash
   dmasif-console restore --backup /var/data/backups/CHOSEN_BACKUP \
     --actor YOUR_OPERATOR_NAME --reason "Recover console history"
   ```

4. Restore reads cluster requests and scheduler evidence, reconstructs webhook deduplication, and stays paused. If evidence remains uncertain, investigate it and rerun `restore` without `--backup`. Never bypass the restore gate or assume an older database means an old job did not run.
5. Once reconciliation succeeds, remove `DMASIF_MAINTENANCE` and redeploy with submissions still disabled. Inspect recovered history. Re-enable the configuration and explicitly `resume` only after operator review.

After any deployment or maintenance downtime, inspect GitHub's delivery history and request redelivery of failed pushes. GitHub does not automatically retry them; saved delivery identities prevent duplicate jobs. [GitHub delivery recovery](https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries)

This repository prepares the application and hosting configuration. No Render service, paid resource, remote adapter installation, or GPU job has been created as part of the local implementation.
