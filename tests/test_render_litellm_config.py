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


# -- multi-nodo / multi-workload (balanceador, nunca antes probado con N>1) -
def test_dos_endpoints_del_mismo_modelo_generan_dos_deployments():
    """El caso central del balanceador: 2 réplicas del mismo workload deben
    aparecer como 2 entradas de model_list con el MISMO model_name -LiteLLM
    hace round-robin/latency-based entre las que comparten nombre."""
    model_list = build_model_list([
        _endpoint(ip="10.0.1.5"),
        _endpoint(ip="10.0.1.6"),
    ])
    assert len(model_list) == 2
    assert {m["model_name"] for m in model_list} == {"sooniverse-qwen3.5"}


def test_dos_endpoints_del_mismo_modelo_tienen_model_info_id_unico():
    """model_info.id debe ser único por nodo -si colisionara, LiteLLM
    trataría dos réplicas distintas como el mismo deployment."""
    model_list = build_model_list([
        _endpoint(ip="10.0.1.5", port=8007),
        _endpoint(ip="10.0.1.6", port=8007),
    ])
    ids = [m["model_info"]["id"] for m in model_list]
    assert len(ids) == len(set(ids)), f"model_info.id duplicado: {ids}"


def test_dos_endpoints_apuntan_a_api_base_distintos():
    model_list = build_model_list([
        _endpoint(ip="10.0.1.5"),
        _endpoint(ip="10.0.1.6"),
    ])
    api_bases = {m["litellm_params"]["api_base"] for m in model_list}
    assert api_bases == {"http://10.0.1.5:8007/v1", "http://10.0.1.6:8007/v1"}


def test_dos_workloads_distintos_generan_model_names_separados():
    """Multi-modelo: qwen3.5 y nemotron en workloads separados no deben
    fusionarse ni pisarse -cada uno mantiene su propio model_name."""
    model_list = build_model_list([
        _endpoint(workload_id="qwen3-5-llm", model_public_name="sooniverse-qwen3.5", ip="10.0.1.5"),
        _endpoint(workload_id="nemotron-llm", model_public_name="sooniverse-nemotron",
                   ip="10.0.1.7", port=8008, hf_repo="cyankiwi/NVIDIA-Nemotron-Nano-9B-v2-AWQ-4bit"),
    ])
    nombres = {m["model_name"] for m in model_list}
    assert nombres == {"sooniverse-qwen3.5", "sooniverse-nemotron"}


def test_dos_workloads_con_replicas_cada_uno_no_mezclan_deployments():
    """Escenario completo: 2 réplicas de qwen3.5 + 1 de nemotron -4 endpoints
    totales, agrupados en exactamente 2 model_name distintos."""
    endpoints = [
        _endpoint(workload_id="qwen3-5-llm", model_public_name="sooniverse-qwen3.5", ip="10.0.1.5"),
        _endpoint(workload_id="qwen3-5-llm", model_public_name="sooniverse-qwen3.5", ip="10.0.1.6"),
        _endpoint(workload_id="nemotron-llm", model_public_name="sooniverse-nemotron",
                   ip="10.0.1.7", port=8008),
    ]
    model_list = build_model_list(endpoints)

    assert len(model_list) == 3
    por_modelo: dict = {}
    for m in model_list:
        por_modelo.setdefault(m["model_name"], []).append(m)
    assert len(por_modelo["sooniverse-qwen3.5"]) == 2
    assert len(por_modelo["sooniverse-nemotron"]) == 1

    ids = [m["model_info"]["id"] for m in model_list]
    assert len(ids) == len(set(ids))


def test_peso_balanceo_distinto_se_propaga_como_weight_por_endpoint():
    model_list = build_model_list([
        _endpoint(ip="10.0.1.5", weight=3),
        _endpoint(ip="10.0.1.6", weight=1),
    ])
    pesos = {m["litellm_params"]["api_base"]: m["litellm_params"]["weight"] for m in model_list}
    assert pesos["http://10.0.1.5:8007/v1"] == 3
    assert pesos["http://10.0.1.6:8007/v1"] == 1


# -- workloads de embeddings: mode: embedding (Fase 4) -----------------------
def test_endpoint_de_embeddings_lleva_mode_embedding():
    """Sin esto, LiteLLM sondea /health con una llamada de CHAT -contra un
    runner de pooling eso falla siempre y el deployment queda 'no sano'."""
    model_list = build_model_list([_endpoint(tipo_tarea="embeddings")])
    assert model_list[0]["model_info"]["mode"] == "embedding"


def test_endpoint_de_texto_no_lleva_mode():
    """'mode' ausente equivale a 'chat' para LiteLLM -no hay que fijarlo
    explícitamente para el caso normal."""
    model_list = build_model_list([_endpoint(tipo_tarea="llm-texto")])
    assert "mode" not in model_list[0]["model_info"]


def test_endpoint_sin_tipo_tarea_no_lleva_mode():
    """Compatibilidad hacia atrás: un endpoint sin 'tipo_tarea' (contrato
    viejo) se comporta igual que 'llm-texto'."""
    model_list = build_model_list([_endpoint()])
    assert "mode" not in model_list[0]["model_info"]


def test_endpoint_de_embeddings_no_lleva_max_output_tokens():
    """Un runner de pooling no genera tokens de salida -max_output_tokens no
    tiene sentido, a diferencia de max_input_tokens (sí aplica)."""
    model_list = build_model_list([_endpoint(tipo_tarea="embeddings", max_model_len=8192)])
    info = model_list[0]["model_info"]
    assert info["max_input_tokens"] == 8192
    assert "max_output_tokens" not in info
