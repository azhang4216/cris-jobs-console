# Install the fixed cluster adapter

This is the operator's future deployment procedure. None of these steps were run during local implementation. They create only the new application root; preserve the existing research checkout, working sandbox, and test outputs.

## Required runtime contract

- The login/GPU nodes need Python 3.10+ for the standard-library helper and batch launcher.
- The batch shell must support `module load apptainer`. Adjust and version the fixed template if the site loads Apptainer differently.
- The frozen image needs the working dMaSIF dependencies, NumPy, CUDA access through `--nv`, and Python compatible with the adapter. Test its actual version and bind/containment flags.
- The research commit must provide `affinity/extract.py`, `affinity/dmasif_compat.py`, `Arguments.py`, `model.py`, their dependencies, and `experiments/run.yaml`.
- The extractor must accept the existing arguments, including an absolute `--ckpt` path, and emit the supported 16-dimensional NPZ feature schema. The current adapter does not install dependencies from research commits.

The image and checkpoint must be immutable registered files with known SHA-256 hashes. A mutable sandbox is not an accepted production image. Freeze a new SIF from the tested environment through the site's approved build process; do not assume an older SIF has all sandbox patches.

## Install a versioned helper release

After obtaining authorization for cluster writes, the operator chooses a new managed root and adapter release directory, for example:

```text
<PROJECT_ROOT>/job_console/
  releases/adapters/v1/
    helper.py
    common.py
    batch.py
    execute.py
    validate.py
    __init__.py
```

Copy these files from `cluster_adapter/` without copying infrastructure secrets or local state. Restrict the new root to the service's cluster identity (`0700` directories, `0600` files). The scripts are invoked by Python and do not require executable file modes.

Set `cluster.root` and `cluster.helper_path` in ignored operator configuration to this root and its `releases/adapters/v1/helper.py`. Configure the account, partition, runtime/checkpoint paths and hashes, input manifest, and trusted SSH endpoint there. No real values belong in this repository's examples.

The staging helper saves a content-addressed copy of its execution modules for each release. Running/queued jobs keep that saved adapter; do not edit an existing release to upgrade it. Install the next release separately and use it only for new accepted requests.

## What the worker does after enabling submission

1. Sends a bounded JSON request over verified SSH to the installed helper. The source archive is carried in that request; large dataset uploads are unsupported.
2. Verifies source/runtime/checkpoint/input hashes, stages private inputs, and saves immutable request evidence.
3. Generates a new batch script with the configured test allocation and unique output/log paths.
4. Creates durable submission/execution claims and saves the scheduler receipt.
5. On the GPU node, verifies retained inputs/source/adapter/runtime again, then mounts source and adapter read-only inside Apptainer.
6. Validates copied result bytes before atomic publication. The worker retrieves only manifest-listed, checksum-verified artifacts within its download limits.

The helper exposes a fixed set of operations through JSON stdin; there is no general shell-command API. Branch names never become shell commands or output paths. Credentials stay on the application host; they are not staged into runs.

## First authorized smoke test

Keep `submissions_enabled: false` until the host, repository webhook, secret mounts, manifests, and runtime are ready. Inspect the generated configuration and helper release first.

Enable the one-input `demo-1stp-v1` preset, submit from one designated run branch, and verify all of these before expanding scope:

- The pinned commit and recorded module paths match the saved source.
- The allocated GPU sees the tested runtime; the checkpoint loads successfully.
- One expected input produces one valid NPZ with nonempty points and 16-dimensional features.
- Dashboard timings, live logs, validation report, and authenticated download work.
- A second config commit repeating the experiment writes to a different UUID/job directory.
- A webhook redelivery links to the original run and does not submit again.

No image build, runtime hash, or real GPU result is claimed by the local simulator. It verifies orchestration and result handling using explicitly synthetic data.

For failed or ambiguous submission, pause new work and use the protected CLI in [deployment.md](deployment.md). Do not remove a claim or run `sbatch` again to make an uncertain run proceed.
