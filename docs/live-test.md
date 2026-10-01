# View the real cluster without paid hosting

Run the application on a laptop and share it through a free temporary Cloudflare Quick Tunnel. No Render service, GitHub Pages site, cloud database, or payment card is needed for this test. SQLite and the SSH key stay on the laptop. The app and tunnel must remain running and the laptop must remain awake.

GitHub Pages serves static files; it cannot run this Python backend, maintain the database, or connect to the cluster over SSH. A Pages frontend would still need a backend elsewhere. [GitHub Pages](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)

## Configure a read-only connection

Copy [config/observe.example.yaml](../config/observe.example.yaml) to ignored `config/local.observe.yaml`. Fill in the SSH host, shared account, project root, private key path, and independently verified `known_hosts` path. Keep this file private. Use a new local state directory, separate from the synthetic demo.

Set `observation_job_prefix` to match the relevant Slurm job names, for example `extract` for `extract_test.sbatch`. The monitor reads only jobs belonging to the configured account, from the past seven days, plus matching currently queued/running jobs. It retains at most 200 jobs per observation; previously cached history stays in the local database.

The `repository` and `allowed_actor_ids` fields remain in the shared config schema. Observation mode does not fetch research code and rejects all webhook submissions, even correctly signed ones. It needs no webhook secret, cluster helper installation, or runtime image configuration.

From the console checkout:

```bash
.venv/bin/dmasif-console --config config/local.observe.yaml observe --once
.venv/bin/dmasif-console --config config/local.observe.yaml serve --host 127.0.0.1 --port 8001
```

Open **http://127.0.0.1:8001**. `serve` starts both the website and one read-only monitor. It cannot submit, cancel, or modify cluster jobs. Ctrl+C stops the local service; existing cluster jobs continue.

The private configuration prepared for this workstation is `.state-cluster-live/config.yaml`. To restart that specific live view:

```bash
.venv/bin/dmasif-console --config .state-cluster-live/config.yaml serve --host 127.0.0.1 --port 8001
```

## Share the live website

In a second terminal on macOS:

```bash
brew install cloudflared
cloudflared tunnel --url http://127.0.0.1:8001 --no-autoupdate
```

Share the HTTPS `trycloudflare.com` URL printed by the tunnel. It is public and requires no viewing login. The URL changes each time the tunnel starts and stops working when it stops. This is a testing setup, without an uptime guarantee. [Cloudflare Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/)

Only the HTTP application is exposed; the browser does not receive the SSH key or a shell connection. The SSH monitor runs locally and all website requests read cached data. No process is installed on the cluster login node.

## Refresh frequency and cost

| Operation | Interval | Work performed |
| --- | --- | --- |
| Browser refresh | `dashboard_poll_seconds: 15` | Read saved status from the laptop's SQLite database. Hidden tabs pause polling. |
| Shared cluster check | `poll_seconds: 60` | One SSH connection runs bounded `sacct` and `squeue` queries for all viewers. Observation mode enforces at least 60 seconds. |
| Elapsed-time display | Every second | Update text in the browser; no network request. |

Four visible tabs produce about 16 dashboard requests per minute, while cluster checks remain approximately once per minute. These checks use no GPU compute. Submitted research jobs use the allocated GPU independently.

If SSH fails, the page retains the last successful observation and shows that updates are delayed. The monitor does not mark jobs failed merely because a connection was lost.

## What imported history can tell us

The view shows real Slurm job names, IDs, states, submission/start/end times, elapsed runtime, and scheduler exit codes. A shared cluster account does not establish which GitHub researcher pushed the job or which commit it used, so those fields stay unknown.

**Completed** means Slurm reported completion. It does not mean output files have been validated. Imported historical logs and result files are not automatically attached: old shared output directories cannot establish which job created a file. **Succeeded** remains reserved for the submission workflow's validated outputs.

An approved manual submission test is separate from this monitor. It should use a fresh source snapshot and output directory, record its Slurm receipt, and validate the produced NPZ. Do not replay a submission after an ambiguous SSH response; reconcile its unique job name/receipt first.

The current preview verifies live observation and a manual Slurm submission. End-to-end GitHub-triggered jobs still require the configured research repository/webhook and the fixed cluster adapter/runtime described in [the deployment guide](cluster-adapter.md).
