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

The lab currently targets **H100 and H200 with the existing runtime**. The authorized October 2 H100 test completed with exit `0:0` in 101 seconds and produced a validated `1STP.npz`; its scientific workflow used one of two allocated GPUs. Blackwell support remains deferred in [TODO.md](../TODO.md).

Set `cluster.gpu_type` in private operator configuration to the site's exact compatible GPU GRES type, such as `nvidia_h100_80gb_hbm3` for an H100. Set the allocation count separately in `presets.quick-test.gpus`: it is a strict integer from one to two, defaulting to one. The adapter requests `--gres=gpu:<type>:<count>`; the GPU type is one bounded token, not a count, constraint expression, or extra scheduler flag. Two GPUs require the explicit supported type `nvidia_h100_80gb_hbm3` or `nvidia_h200`. Omitted or `null` GPU type remains valid only for count one and requests `--gres=gpu:1`, permitting any model in the partition. Both settings are saved with each accepted run and cannot be set in researcher YAML. The helper independently checks them before staging. [Slurm typed GPU requests](https://slurm.schedmd.com/gres.html)

**A typed GPU request is not proof of the allocation.** Read-only inspection after job `112108` found that this site's installed `job_submit.lua` deliberately rewrites a single-GPU H100 request to an untyped request with an RTX 6000 feature constraint. The saved request and batch script correctly specified `gpu:nvidia_h100_80gb_hbm3:1`, but Slurm's allocation record and the execution log both confirmed Blackwell. Changing the branch name, repeating that flag, or adding an H100 constraint does not resolve this policy. Job-submit plugins can modify requests after submission. [Slurm submission plugins](https://slurm.schedmd.com/job_submit_plugins.html)

The current PyTorch 2.5.0a0/CUDA 12.6 image failed on Blackwell with an unsupported architecture warning and a KeOps CUDA image error. H100/H200 use compute capability 9.0; RTX PRO 6000 Blackwell uses 12.0. The current scheduler preserves supported typed requests for two GPUs. On October 2, after initially choosing to wait for a one-GPU route, the user authorized one isolated test reserving two H100 GPUs while extraction uses one. That test passed on H100; it was an explicit resource decision, not an automatic retry. Further submissions remain paused, and the operator default is restored to one. The existing runtime and both failed runs remain intact. Routine one-GPU H100/H200 allocation still needs a supported route. [NVIDIA GPU capabilities](https://developer.nvidia.com/cuda/gpus)

Blackwell needs a separately tested runtime: CUDA 12.8 added native `sm_120` compilation, and PyTorch 2.7 with CUDA 12.8 introduced explicit Blackwell support. Older builds can sometimes use compatible PTX, but that does not establish this runtime's compatibility. Validate both PyTorch and KeOps on the allocated GPU, freeze a new image/hash when upgrading, and use a new run; a CPU-only startup check is insufficient. [CUDA 12.8 features](https://docs.nvidia.com/cuda/archive/12.8.2/cuda-features-archive/index.html), [PyTorch Blackwell support](https://pytorch.org/blog/pytorch-2-7/), [NVIDIA compatibility guide](https://docs.nvidia.com/cuda/blackwell-compatibility-guide/index.html)

The adapter retains successful submission warnings in the private receipt; the worker displays a redacted warning in the run's activity. Before extraction, the adapter checks CUDA availability, one visible device, and the exact model/capability for the three mapped site types (H100, H200, RTX PRO 6000 Blackwell). A known-type mismatch fails with a clear log message and private `preflight.json`; it does not publish scientific results. Untyped or unmapped one-GPU requests remain usable but explicitly report an unverified model match. This check establishes device identity only, not working PyTorch/KeOps kernels. Immutable helper release `v1-b419bd4bd0e7` is installed and activated for the authorized test; existing runs retain their original adapter identities. The Blackwell identity mapping is a mismatch guard, not validated Blackwell runtime support.

The launcher validates that Slurm's `CUDA_VISIBLE_DEVICES` contains exactly the requested count of unique numeric IDs or full GPU UUIDs. It saves the assigned mask in private execution evidence, then explicitly forwards only the first assigned device through Apptainer's clean environment. The extractor still uses a single visible device as `cuda:0`; reserving two does not enable distributed or multi-GPU extraction. Missing, duplicate, count-mismatched, malformed, and MIG masks fail before launch. A valid `CUDA_DEVICE_ORDER` is preserved. Never replace the scheduler's device ID with a guessed host index: Slurm can remap device numbering. [Apptainer GPU selection](https://apptainer.org/docs/user/1.1/gpu.html#multiple-gpus), [Slurm GPU management](https://slurm.schedmd.com/gres.html#GPU_Management)

The saved stage provenance records `requested_gpu_count`. Execution and result evidence add `allocated_gpu_count` from the validated scheduler mask and `used_gpu_count` for the single-device extractor. The container verifies these counts against the accepted request and checks that its device mask matches the host's selected device. Public details expose the available integer counts; device masks, UUIDs, and private paths remain private. While a job is pending, its requested count is known but allocation and use are not yet established. Existing historical records without these fields remain readable.

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
- Requested, allocated, and used GPU counts match the approved test: two, two, and one for the isolated October 2 reservation.
- One expected input produces one valid NPZ with nonempty points and 16-dimensional features.
- Dashboard timings, live logs, validation report, and public download work.
- A second config commit repeating the experiment writes to a different UUID/job directory.
- A webhook redelivery links to the original run and does not submit again.

No image build, runtime hash, or real GPU result is claimed by the local simulator. It verifies orchestration and result handling using explicitly synthetic data.

For failed or ambiguous submission, pause new work and use the protected CLI in [deployment.md](deployment.md). Do not remove a claim or run `sbatch` again to make an uncertain run proceed.
