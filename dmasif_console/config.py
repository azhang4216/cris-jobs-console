"""Strict operator configuration. Research commits cannot change these policies."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryConfig(StrictModel):
    id: int = Field(gt=0)
    full_name: str
    clone_url: str

    @field_validator("full_name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
            raise ValueError("repository full_name must be owner/repository")
        return value

    @field_validator("clone_url")
    @classmethod
    def no_embedded_secret(cls, value: str) -> str:
        if re.match(r"https?://[^/]*@", value):
            raise ValueError("use a credential environment variable, never a token in clone_url")
        return value

    @property
    def url(self) -> str:
        return f"https://github.com/{self.full_name}"


class InputEntry(StrictModel):
    path: str
    sha256: str
    chains: str | None = None

    @field_validator("sha256")
    @classmethod
    def digest(cls, value: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("input sha256 must be a lowercase SHA-256 digest")
        return value


class DatasetConfig(StrictModel):
    entries: list[InputEntry] = Field(min_length=1, max_length=16)
    merge_models: bool = False
    description: str = ""


class PresetConfig(StrictModel):
    gpus: Literal[1] = 1
    cpus: int = Field(default=8, ge=1, le=8)
    memory_gb: int = Field(default=64, ge=1, le=64)
    wall_minutes: int = Field(default=15, ge=1, le=15)
    qos: Literal["test"] = "test"
    max_atoms: int = Field(default=150_000, ge=1, le=150_000)
    max_proteins: int = Field(default=16, ge=1, le=16)


class ClusterConfig(StrictModel):
    host: str = "login.example.invalid"
    user: str = "researcher"
    root: str = "/srv/lab/job_console"
    helper_path: str = "/srv/lab/job_console/releases/adapters/v1/helper.py"
    ssh_key_path: Path | None = None
    known_hosts_path: Path | None = None
    account: str = "lab-account"
    partition: str = "gpu"
    qos: Literal["test"] = "test"
    cpus: int = 8
    memory_gb: int = 64
    wall_minutes: int = 15
    runtime_image: str = ""
    runtime_sha256: str = ""
    runtime_unsquash: bool = Field(default=False, strict=True)
    checkpoint_path: str = ""
    checkpoint_sha256: str = ""
    adapter_version: str = "v1"
    ssh_timeout_seconds: int = Field(default=30, ge=1, le=300)

    @field_validator("host", "user", "account", "partition", "adapter_version")
    @classmethod
    def safe_token(cls, value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
            raise ValueError("unsafe cluster identifier")
        return value

    @field_validator("root", "helper_path", "runtime_image", "checkpoint_path")
    @classmethod
    def safe_remote_path(cls, value: str) -> str:
        if value and (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value) or ".." in Path(value).parts):
            raise ValueError("remote paths must be absolute and contain only safe path characters")
        return value

    @field_validator("runtime_sha256", "checkpoint_sha256")
    @classmethod
    def optional_digest(cls, value: str) -> str:
        if value and not re.fullmatch(r"[a-f0-9]{64}", value):
            raise ValueError("expected a lowercase SHA-256 digest")
        return value

    def public_snapshot(self) -> dict:
        return self.model_dump(mode="json", exclude={"ssh_key_path", "known_hosts_path"})


class ExperimentConfig(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    schema_version: Literal[1] = 1
    job_type: Literal["dmasif_extract"] = "dmasif_extract"
    dataset_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    preset_id: Literal["quick-test"] = "quick-test"
    seed: int = Field(default=0, ge=0, le=2**32 - 1)
    repeat_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_schema_version(cls, value):
        # Literal[1] also matches True and 1.0, even under strict validation.
        if type(value) is not int:
            raise ValueError("schema_version must be the integer 1")
        return value


class Settings(StrictModel):
    state_dir: Path = Path(".state")
    mode: Literal["fake", "ssh", "observe"] = "fake"
    repository: RepositoryConfig
    allowed_actor_ids: list[int] = Field(min_length=1)
    hook_id: str = "dmasif"
    operator_contact: str = "Ask your lab operator"
    # Cluster checks are independent of cheap browser reads from the local DB.
    poll_seconds: int = Field(default=30, ge=1, le=300)
    dashboard_poll_seconds: int = Field(default=15, ge=3, le=300)
    observation_job_prefix: str = Field(default="dmasif", min_length=1, max_length=100,
                                        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
    submissions_enabled: bool = False
    # Legacy settings remain readable so existing deployments can upgrade.
    # They never gate viewing; retain the old password only for log redaction.
    viewer_username: str = Field(default="lab", exclude=True)
    viewer_password: str = Field(default="", repr=False, exclude=True)
    webhook_secret: str = Field(default="", repr=False, exclude=True)
    datasets: dict[str, DatasetConfig] = Field(default_factory=dict)
    presets: dict[str, PresetConfig] = Field(default_factory=lambda: {"quick-test": PresetConfig()})
    cluster: ClusterConfig = Field(default_factory=ClusterConfig)
    queue_limit: int = Field(default=100, ge=1, le=1000)
    max_webhook_bytes: int = Field(default=2 * 1024 * 1024, ge=1024, le=25 * 1024 * 1024)
    max_source_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_input_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_artifact_bytes: int = Field(default=10 * 1024 * 1024, ge=1024)
    max_run_artifact_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_cache_bytes: int = Field(default=1024**3, ge=1024)
    min_free_bytes: int = Field(default=256 * 1024 * 1024, ge=0)
    log_tail_bytes: int = Field(default=64 * 1024, ge=1024, le=1024 * 1024)

    @model_validator(mode="after")
    def production_policy(self) -> "Settings":
        if any(type(actor) is not int or actor <= 0 for actor in self.allowed_actor_ids):
            raise ValueError("allowed actors must be positive numeric GitHub IDs")
        if set(self.presets) != {"quick-test"}:
            raise ValueError("v1 supports the quick-test preset only")
        if self.mode == "observe":
            if self.submissions_enabled:
                raise ValueError("Observe mode is read-only; submissions_enabled must be false")
            if not self.cluster.ssh_key_path or not self.cluster.known_hosts_path:
                raise ValueError("Observe mode requires an SSH key and pinned known-hosts file")
        if self.mode == "ssh":
            if self.repository.clone_url != self.repository.url + ".git":
                raise ValueError("SSH mode must fetch the configured GitHub repository over HTTPS")
            for name in ("runtime_image", "runtime_sha256", "checkpoint_path", "checkpoint_sha256"):
                if not getattr(self.cluster, name):
                    raise ValueError(f"SSH mode requires the tested cluster {name}")
            if not self.operator_contact.strip() or self.operator_contact == "Ask your lab operator":
                raise ValueError("configure a named operator/contact before deployment")
        return self

    @property
    def database_path(self) -> Path:
        return self.state_dir / "console.sqlite3"

    def policy_snapshot(self) -> dict:
        return {
            "datasets": {k: v.model_dump(mode="json") for k, v in self.datasets.items()},
            "presets": {k: v.model_dump(mode="json") for k, v in self.presets.items()},
            "cluster": self.cluster.public_snapshot(),
            "repository": self.repository.model_dump(mode="json"),
        }


def _secret(name: str) -> str | None:
    direct = os.environ.get(name)
    file_name = os.environ.get(name + "_FILE")
    if direct is not None and file_name:
        raise ValueError(f"set either {name} or {name}_FILE, not both")
    return Path(file_name).read_text().rstrip("\r\n") if file_name else direct


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("DMASIF_CONFIG", "config/local.yaml"))
    raw = path.read_text()
    if len(raw) > 1024 * 1024:
        raise ValueError("operator configuration too large")
    data = yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise ValueError("operator configuration must be a mapping")
    for env, field in (("DMASIF_WEBHOOK_SECRET", "webhook_secret"), ("DMASIF_VIEWER_PASSWORD", "viewer_password")):
        secret = _secret(env)
        if secret is not None:
            data[field] = secret
    if os.environ.get("DMASIF_STATE_DIR"):
        data["state_dir"] = os.environ["DMASIF_STATE_DIR"]
    for env, field in (("DMASIF_SSH_KEY", "ssh_key_path"), ("DMASIF_KNOWN_HOSTS", "known_hosts_path")):
        if os.environ.get(env):
            data.setdefault("cluster", {})[field] = os.environ[env]
    settings = Settings.model_validate(data)
    settings.state_dir = settings.state_dir.expanduser().resolve()
    return settings
