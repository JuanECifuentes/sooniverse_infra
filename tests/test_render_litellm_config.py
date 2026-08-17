"""
Pruebas de scripts/render_litellm_config.py::build_model_list.

Cubre el bug de causa raíz reportado ("el chat falla al hablar con el LLM"):
la versión anterior fijaba litellm_params.max_tokens = max_model_len, el
mismo valor que --max-model-len de vLLM. Como max_tokens es presupuesto de
SALIDA, cualquier prompt no vacío hacía que prompt_tokens + max_tokens
superara el context window real y vLLM devolviera 400 en cada mensaje.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from render_litellm_config import build_model_list  # noqa: E402


def _endpoint(**overrides):
    base = {
        "workload_id": "qwen3-5-llm",
        "model_public_name": "sooniverse-qwen3.5",
        "hf_repo": "cyankiwi/Qwen3.5-2B-AWQ-4bit",
        "ip": "10.0.1.5",
        "port": 8007,
        "weight": 1,
        "max_model_len": 16384,
        "capacidades": {"vision": True, "tool_calling": False},
    }
    base.update(overrides)
    return base


def test_litellm_params_never_sets_output_max_tokens():
    """El bug de causa raíz: max_tokens NO debe aparecer en litellm_params."""
    model_list = build_model_list([_endpoint()])
    assert "max_tokens" not in model_list[0]["litellm_params"]


def test_model_info_carries_context_window_instead():
    model_list = build_model_list([_endpoint(max_model_len=16384)])
    info = model_list[0]["model_info"]
    assert info["max_input_tokens"] == 16384
    assert info["max_output_tokens"] == 4096  # min(4096, 16384 // 4)


def test_model_info_output_cap_scales_down_for_small_context():
    model_list = build_model_list([_endpoint(max_model_len=2048)])
    info = model_list[0]["model_info"]
    assert info["max_output_tokens"] == 512  # min(4096, 2048 // 4)


def test_model_info_exposes_capability_flags_for_litellm():
    model_list = build_model_list([_endpoint(capacidades={"vision": True, "tool_calling": True})])
    info = model_list[0]["model_info"]
    assert info["supports_vision"] is True
    assert info["supports_function_calling"] is True
    assert info["sooniverse_capabilities"] == {"vision": True, "tool_calling": True}


def test_endpoint_without_max_model_len_omits_context_fields():
    model_list = build_model_list([_endpoint(max_model_len=None)])
    info = model_list[0]["model_info"]
    assert "max_input_tokens" not in info
    assert "max_output_tokens" not in info


def test_endpoint_without_ip_is_skipped():
    model_list = build_model_list([_endpoint(ip=None)])
    assert model_list == []
