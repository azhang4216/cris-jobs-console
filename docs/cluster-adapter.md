# Install the fixed cluster adapter

Use this procedure to install a managed release in a new application root. Preserve the existing research checkout, working sandbox, and test outputs. The live submission setup and acceptance checks are described in [live-submissions.md](live-submissions.md).

## Required runtime contract

- The login/GPU nodes need Python 3.10+ for the standard-library helper and batch launcher.
- The batch shell must support `module load apptainer`. Adjust and version the fixed template if the site loads Apptainer differently.
- The frozen image needs the working dMaSIF dependencies, NumPy, CUDA access through `--nv`, and Python compatible with the adapter. Test its actual version and bind/containment flags.
- The research commit must provide `affinity/extract.py`, `affinity/dmasif_compat.py`, `Arguments.py`, `model.py`, their dependencies, and `experiments/run.yaml`.
- The extractor must accept the existing arguments, including an absolute `--ckpt` path, and emit the supported 16-dimensional NPZ feature schema. The current adapter does not install dependencies from research commits.

The image and checkpoint must be immutable registered files with known SHA-256 hashes. A mutable sandbox is not an accepted production image. Freeze a new SIF from the tested environment through the site's approved build process; do not assume an older SIF has all sandbox patches.

If the site's Apptainer cannot mount SIF files because squashfuse is unavailable, the operator can set `cluster.runtime_unsquash: true`. This adds Apptainer's `--unsquash` flag after verifying the same pinned SIF checksum; it does not permit a mutable sandbox as the registered runtime. Keep the default `false` where SIF mounting works. This flag belongs in private operator configuration, never in a researcher's `experiments/run.yaml`.

With unpacking enabled, each allocation uses a new private temporary directory under `SLURM_TMPDIR`, or `/tmp` if the scheduler does not set it. Verify on the compute nodes that this root is local disk, has enough free inodes, and can hold the uncompressed image. The runner checks for at least **four times the SIF size plus 1 GiB** free before launching; this is a conservative minimum, not an exact expansion estimate or a reservation. In particular, a RAM-backed `/tmp` may be unsuitable. Allow enough job time for extraction. Results and scientific caches remain in their retained per-job directories; the host's home setting stays unchanged. Temporary unpacked files are removed after ordinary completion, launch errors, or runtime failure. SIGKILL, node loss, or a hard scheduler termination can prevent cleanup, so the site's scheduler scratch-cleanup policy is still needed.

## Match the runtime to the GPU

Set `cluster.gpu_type` in private operator configuration to the site's exact compatible GPU GRES type, such as `nvidia_h100_80gb_hbm3` for an H100. The adapter requests `--gres=gpu:<type>:1`; the value is one bounded token, not a GPU count, constraint expression, or extra scheduler flag. Omitted or `null` keeps `--gres=gpu:1`, which permits any model in the partition. This setting is saved with each accepted run and cannot be set in researcher YAML. [Slurm typed GPU requests](https://slurm.schedmd.com/gres.html)

For the current PyTorch 2.5.0a0/CUDA 12.6 runtime, first repeat acceptance on an H100 (`nvidia_h100_80gb_hbm3`), preserving the existing image and failed-run evidence. H100/H200 use compute capability 9.0; RTX PRO 6000 Blackwell uses 12.0. The observed Blackwell attempt reported an unsupported architecture and a KeOps CUDA image error. [NVIDIA GPU capabilities](https://developer.nvidia.com/cuda/gpus)

Blackwell needs a separately tested runtime: CUDA 12.8 added native `sm_120` compilation, and PyTorch 2.7 with CUDA 12.8 introduced explicit Blackwell support. Older builds can sometimes use compatible PTX, but that does not establish this runtime's compatibility. Validate both PyTorch and KeOps on the allocated GPU, freeze a new image/hash when upgrading, and use a new run; a CPU-only startup check is insufficient. [CUDA 12.8 features](https://docs.nvidia.com/cuda/archive/12.8.2/cuda-features-archive/index.html), [PyTorch Blackwell support](https://pytorch.org/blog/pytorch-2-7/), [NVIDIA compatibility guide](https://docs.nvidia.com/cuda/blackwell-compatibility-guide/index.html)

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

The staging helper saves a content-addressed copy of its execution modules for each release. Running/queued jobs keep that saved adapter; do not edit an existing release to upgrade it. Install the next release separately; its execution changes apply to newly accepted requests.

Read-only monitoring may use the current installed helper for older runs when their SSH host, user, and managed root exactly match current configuration. This allows accounting fixes to recover old statuses without changing accepted policy or execution provenance. Staging, submission, and cancellation still use the run's pinned release. Accounting matches owner, full UUID job name, and known allocation IDs; an absent stored comment is allowed, but a conflicting comment is rejected. Slurm stores that comment only when the site enables `AccountingStoreFlags=job_comment`. [Slurm accounting fields](https://slurm.schedmd.com/sacct.html)

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
- Dashboard timings, live logs, validation report, and public download work.
- A second config commit repeating the experiment writes to a different UUID/job directory.
- A webhook redelivery links to the original run and does not submit again.

No image build, runtime hash, or real GPU result is claimed by the local simulator. It verifies orchestration and result handling using explicitly synthetic data.

For failed or ambiguous submission, pause new work and use the protected CLI in [deployment.md](deployment.md). Do not remove a claim or run `sbatch` again to make an uncertain run proceed.
