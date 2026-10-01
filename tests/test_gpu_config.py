"""GPU compatibility is an operator policy, never an arbitrary job directive."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from dmasif_console.config import ClusterConfig, ExperimentConfig, Settings


@pytest.mark.parametrize("gpu_type", [None, "nvidia_h100_80gb_hbm3", "nvidia_h200",
                                     "rtx_pro_6000_blackwell", "GPU-test_1", "a" * 100])
def test_gpu_type_accepts_one_bounded_operator_token(gpu_type):
    assert ClusterConfig(gpu_type=gpu_type).gpu_type == gpu_type


def test_gpu_type_default_keeps_existing_untyped_configurations_readable():
    assert ClusterConfig().gpu_type is None


@pytest.mark.parametrize("gpu_type", ["", " ", "a" * 101, "gpu:h100:2", "h100,h200", "h100|h200",
                                     "h100\n#SBATCH --gres=gpu:8", "h100\n", "h100\r", "h100\t",
                                     "h100 --exclusive", "h100;false", "$(false)", "--exclusive",
                                     "h100/other", "h100.1", "h１００", True, False, 1, 1.5,
                                     b"h100", ["h100"], {"type": "h100"}])
def test_gpu_type_rejects_coercion_counts_expressions_and_directives(gpu_type):
    with pytest.raises(ValidationError):
        ClusterConfig(gpu_type=gpu_type)


def test_gpu_type_is_preserved_in_accepted_policy_snapshot(tmp_path):
    settings = Settings(
        state_dir=tmp_path / "state", repository={"id": 1, "full_name": "lab/research", "clone_url": str(tmp_path)},
        allowed_actor_ids=[1], cluster={"gpu_type": "nvidia_h100_80gb_hbm3"},
    )
    accepted = settings.policy_snapshot()
    settings.cluster.gpu_type = "nvidia_h200"
    assert accepted["cluster"]["gpu_type"] == "nvidia_h100_80gb_hbm3"
    assert settings.policy_snapshot()["cluster"]["gpu_type"] == "nvidia_h200"


def test_researcher_yaml_cannot_override_gpu_selection():
    with pytest.raises(ValidationError) as exc:
        ExperimentConfig.model_validate({"dataset_id": "demo-1stp-v1", "repeat_id": "one",
                                         "gpu_type": "rtx_pro_6000_blackwell"})
    assert [(error["loc"], error["type"]) for error in exc.value.errors()] == [(("gpu_type",), "extra_forbidden")]
