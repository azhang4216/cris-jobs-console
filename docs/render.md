# Deploy one Render application

This is an optional paid deployment. The current free live test uses the laptop and a temporary tunnel; see [live-test.md](live-test.md).

If deployed on Render, use one web service and one persistent disk. The service hosts the website, watches the job queue, and keeps SQLite history and small downloadable results on the disk. The cluster still runs dMaSIF and retains large outputs.

Researchers only need the dashboard URL and their personal GitHub account. The website is public and needs no login. A push to `runs/<experiment>`, such as `runs/pocket-v1`, records the exact commit and creates a new run with its own output directory. The signed webhook supplies the pushing researcher's login and numeric ID automatically; branch names do not identify their owner. There is no website submission form or researcher account.

## Starting cost and size

The Blueprint starts with `0.5c-512mb` (formerly Starter): 0.5 CPU and 512 MB RAM. As of October 2, 2026, compute is $7/month and the 10-GB persistent disk is $2.50/month, for **about $9.50/month** on a $0 Hobby workspace, before taxes and usage extras. The previous `1c-2g` choice is $25/month plus the same disk. See [Render pricing](https://render.com/pricing) and [compute plans](https://render.com/docs/compute-plans).

This small tier is a starting point for the four-researcher pilot, not a measured production memory requirement. The service runs the web process, one worker, and a supervisor; GPU computation runs on the cluster. Check Render memory metrics through source preparation, submission, monitoring, downloads, and backup before enabling routine use. Source archives are buffered during SSH transfer, so large repositories can need more memory even with few users. If the service runs out of memory, use `1c-2g` and reconcile any interrupted submission before proceeding.

An October 2 local test of the production Docker image passed signed synthetic submission, invalid-config rejection, result download, backup, and restart recovery with a 512-MiB memory limit, no swap, and 0.5 CPU. Its cgroup memory peak was about 201 MiB with no out-of-memory events. It used tiny synthetic inputs and local CPU emulation of the deployment architecture; this does not establish memory requirements for large archives or verify real SSH access from Render.

Keep the disk: it holds SQLite history and saved sources. [Render's free web tier](https://render.com/docs/free) cannot attach a persistent disk and sleeps after 15 minutes without inbound traffic, so it cannot host this design reliably. No separate paid database or worker service is needed. A Hobby workspace has one administrator seat; researchers can still use GitHub and the public dashboard without Render accounts. Additional Render administrators require a workspace plan that supports them.

## Set up once

1. **Connect the infrastructure repository**, `azhang4216/cris-jobs-console`, to Render. Keep its `main` branch protected and separate from the dMaSIF research repository. Before publishing any changes, check Git's file list; private configuration, keys, notes, and state must stay ignored.
2. **Prepare the private operator configuration.** Copy [config/example.yaml](../config/example.yaml) to ignored `deploy/private/operator.yaml`, or adapt the verified local pilot configuration. Fill in the approved research repository and numeric researcher IDs, cluster settings, operator contact, dataset checksums, and tested runtime/checkpoint hashes. Keep `submissions_enabled: false`. The fixed cluster adapter and runtime must be installed and tested before enabling jobs; see [cluster setup](cluster-adapter.md). These instructions do not install anything remotely.
3. **Create a Render Blueprint** from the infrastructure repository. [render.yaml](../render.yaml) requests a paid `0.5c-512mb` web service in Virginia, one instance, and a 10-GB disk. Review the displayed recurring charge before creating resources.
4. **Fill in Render's secret prompts** using the table below. The Blueprint declares only the names with `sync: false`; paste actual values into Render, never into `render.yaml`, GitHub, application logs, or chat. For an existing Blueprint, add these variables manually under **Environment → Environment Variables**, because Render prompts only when a Blueprint is first created. Save and deploy after all values are present. See [Render's secret-prompt behavior](https://render.com/docs/blueprint-spec#prompting-for-secret-values).
5. **Open the Render HTTPS URL**; no login is required. Configure the research repository's push webhook using the table below. Keep submissions disabled until the authorized cluster smoke test is ready.

| Render environment variable | Value to paste | Purpose |
| --- | --- | --- |
| `DMASIF_OPERATOR_YAML` | Complete private operator YAML | Cluster hostname, SSH username, account, paths, approved researchers, and tested runtime/data settings |
| `DMASIF_SSH_PRIVATE_KEY` | Complete approved SSH private key, including BEGIN/END lines and original line breaks | Unattended cluster authentication |
| `DMASIF_SSH_KNOWN_HOSTS` | Independently verified `known_hosts` entry | Verify the cluster's identity when connecting |
| `DMASIF_WEBHOOK_SECRET` | A long random secret, also configured in the research repository's webhook | Authenticate GitHub push deliveries |
| `DMASIF_REPOSITORY_TOKEN` | Fine-grained GitHub token with **Contents: Read-only**, restricted to `dmasif-experiments` | Fetch exact source commits from the private research repository |

The Docker startup command writes the supplied configuration, key, host entry, and repository token to owner-only files in `/home/console/.config/dmasif-console/` and removes those raw values from the environment inherited by the application. They are outside the persistent state, backups, and built image. The Blueprint already sets `DMASIF_CONFIG`, `DMASIF_SSH_KEY`, `DMASIF_KNOWN_HOSTS`, and `DMASIF_REPOSITORY_TOKEN_FILE` to the generated paths. **Those four variables hold paths; do not replace their values with file contents.** Missing required secrets stop startup without logging their values.

GitHub Actions secrets do not transfer automatically to Render. Use the operator configuration schema rather than pasting raw login notes. SSH currently uses port 22. If Render's initial form does not preserve multiline values, add them in the service's Environment editor, or use the Secret Files alternative below; do not turn real line breaks into literal `\n` text.

The key's extension does not matter. The key must be valid and usable without an interactive passphrase prompt. Each SSH call uses a temporary owner-readable copy of the mounted key, which is removed afterward; never make a private key public to fix SSH permissions.

| GitHub webhook setting | Value |
| --- | --- |
| Payload URL | `https://YOUR_SERVICE.onrender.com/webhooks/github` |
| Content type | `application/json` |
| Secret | The same value as Render's `DMASIF_WEBHOOK_SECRET` |
| Events | Push events only |
| SSL verification | Enabled |

Confirm that the cluster allows incoming SSH from the service's [Render outbound IP ranges](https://render.com/docs/outbound-ip-addresses). If it requires an institutional VPN or restricted source addresses, the site operator must arrange access first. No real connection has been verified from Render yet.

When moving the existing laptop pilot, preserve its history using the application's backup and restore procedure below before switching the GitHub webhook. Stop the old service's worker before starting the replacement worker; do not run two independent submission services against the same cluster run directory. Leave the Render webhook disconnected until restored history and reconciliation have been checked.

Hosting on Render does not change the cluster's GPU allocation policy. The successful [H100 acceptance test](acceptance-2026-10-01.md) reserved two GPUs with permission for that test only; routine submissions remain paused while an approved allocation route is decided. Do not enable the default one-GPU preset assuming it will receive an H100/H200.

Once the allocation route and run are authorized, set `submissions_enabled: true` in Render's `DMASIF_OPERATOR_YAML` value and redeploy. In **Render → Shell**, clear any stored pause:

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
| `/home/console/.config/dmasif-console/` | Generated private runtime files; excluded from application backups |

Keep one instance and the disk mounted at `/var/data`. Only this mount persists. Run operator commands in the live service's **Shell**, not a Render one-off job or pre-deploy command, because those do not have the disk. See [Render's disk requirements](https://render.com/docs/disks).

`render.yaml` disables automatic application deployment and previews are not enabled. Research pushes only reach the signed webhook; they cannot deploy infrastructure. Operators manually deploy reviewed `main` revisions. Render supplies the HTTPS endpoint, so there is no separate proxy, DNS setup, or Pages deployment required.

## Private research repository

The Blueprint includes `DMASIF_REPOSITORY_TOKEN` and the fixed credential helper settings. Restrict that token to reading the research repository's contents. Do not put it in the clone URL. Render's access to build the infrastructure repository does not automatically grant the running application access to the separate research repository. If deploying against a public research repository, omit the token prompt from your Blueprint and leave the token unset.

## Secret Files alternative

Existing file-based deployments remain supported. If you prefer Render's **Environment → Secret Files**, remove the corresponding raw-content environment variables and add these files instead:

| Secret filename | Path environment variable | Value |
| --- | --- | --- |
| `operator.yaml` | `DMASIF_CONFIG` | `/etc/secrets/operator.yaml` |
| `cluster-key` | `DMASIF_SSH_KEY` | `/etc/secrets/cluster-key` |
| `known-hosts` | `DMASIF_KNOWN_HOSTS` | `/etc/secrets/known-hosts` |
| `repository-token` | `DMASIF_REPOSITORY_TOKEN_FILE` | `/etc/secrets/repository-token` |

Keep `DMASIF_WEBHOOK_SECRET`, `GIT_ASKPASS`, and `GIT_TERMINAL_PROMPT` as configured by the Blueprint. Update the path entries in your Blueprint to match this alternative before syncing it again. Do not combine a raw-content variable with an `/etc/secrets` destination: the startup command never overwrites Render-mounted secret files. Edit `operator.yaml` for subsequent configuration changes when using this alternative.

## Secrets and access

Dashboard pages, run APIs, logs, and result downloads are public. Keeping either GitHub repository private protects that repository's source, not the website. A commit link still requires repository access to view the code on GitHub. Visitors cannot submit, cancel, or retry runs; the receiver requires a valid GitHub signature and an approved repository and numeric sender ID.

The browser never receives the SSH key, private operator configuration, or repository token. Render administrators and the backend can access the service's secrets, so only trusted operators should administer it. The web and worker share one service and operating-system user; this design does not isolate the key from a compromised web process.

Render stores the submitted environment values, and authorized service administrators can access them. Removing raw values from the application environment does not remove them from Render's settings. Dockerfile instructions never reference credential build arguments. If using [Render Secret Files](https://render.com/docs/configure-environment-variables#secret-files), Render also places copies in the Docker build context; our Dockerfile-specific allowlist excludes them. Preserve these rules when editing the image; never replace explicit `COPY` instructions with an unrestricted `COPY . .`. See [Render's Docker secret handling](https://render.com/docs/docker).

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

The local pilot has installed an isolated cluster adapter and completed a real GPU run; see the [acceptance record](acceptance-2026-10-01.md). No Render deployment or connection from Render to the cluster has been verified yet.
