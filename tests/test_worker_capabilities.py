"""
Pruebas de las capacidades por workload (config_global.yaml -> envs de
SkyPilot -> flags de vLLM). No requiere AWS ni PostgreSQL.
"""

import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_infra import TopologyBuilder  # noqa: E402


def load_base_config():
    with (REPO_ROOT / "config_global.yaml").open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def clone(config):
    return yaml.safe_load(yaml.dump(config))


def test_default_capabilities_match_base_config():
    cfg = load_base_config()
    builder = TopologyBuilder(cfg)
    envs = builder.build_worker(cfg["workloads"][0])["envs"]

    assert envs["ENABLE_VISION"] == "1"
    assert envs["ENABLE_TOOL_CALLING"] == "0"
    assert envs["TOOL_CALL_PARSER"] == ""


def test_tool_calling_enabled_propagates_parser():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["capacidades"]["tool_calling"] = True
    cfg["workloads"][0]["capacidades"]["tool_call_parser"] = "hermes"
    builder = TopologyBuilder(cfg)
    envs = builder.build_worker(cfg["workloads"][0])["envs"]

    assert envs["ENABLE_TOOL_CALLING"] == "1"
    assert envs["TOOL_CALL_PARSER"] == "hermes"


def test_vision_disabled_propagates():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["capacidades"]["vision"] = False
    builder = TopologyBuilder(cfg)
    envs = builder.build_worker(cfg["workloads"][0])["envs"]

    assert envs["ENABLE_VISION"] == "0"


def test_workload_without_capacidades_defaults_to_current_behavior():
    """Un workload sin 'capacidades' (contratos viejos) debe comportarse igual
    que antes de este cambio: visión activa, tool calling inactivo."""
    cfg = clone(load_base_config())
    del cfg["workloads"][0]["capacidades"]
    builder = TopologyBuilder(cfg)
    envs = builder.build_worker(cfg["workloads"][0])["envs"]

    assert envs["ENABLE_VISION"] == "1"
    assert envs["ENABLE_TOOL_CALLING"] == "0"
