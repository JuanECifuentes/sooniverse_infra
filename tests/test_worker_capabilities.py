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


# -- concurrencia del planificador de vLLM ------------------------------------
def test_concurrencia_del_contrato_llega_a_los_envs():
    cfg = load_base_config()
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["MAX_NUM_SEQS"] == "16"
    assert envs["MAX_NUM_BATCHED_TOKENS"] == "8192"


def test_concurrencia_override_se_propaga():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["concurrencia"] = {"max_num_seqs": 32, "max_num_batched_tokens": 16384}
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["MAX_NUM_SEQS"] == "32"
    assert envs["MAX_NUM_BATCHED_TOKENS"] == "16384"


def test_workload_sin_seccion_concurrencia_usa_los_defaults():
    """Un contrato anterior a esta sección no debe quedarse con el viejo
    max_num_seqs=2 del entrypoint, que era el techo real del sistema."""
    cfg = clone(load_base_config())
    del cfg["workloads"][0]["concurrencia"]
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["MAX_NUM_SEQS"] == "16"
    assert envs["MAX_NUM_BATCHED_TOKENS"] == "8192"


def test_run_script_exporta_la_concurrencia():
    """Sin estos export, los envs de SkyPilot no llegan a 'docker compose' y
    vLLM arrancaría con los defaults del entrypoint."""
    cfg = load_base_config()
    run = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["run"]
    assert 'export MAX_NUM_SEQS="${MAX_NUM_SEQS}"' in run
    assert 'export MAX_NUM_BATCHED_TOKENS="${MAX_NUM_BATCHED_TOKENS}"' in run


# -- puerto, TENSOR_PARALLEL_SIZE y overrides de bajo nivel de vLLM ----------
# (Fase 1/5 del plan Azure-T4: multi-GPU roto y puerto acoplado a la imagen
# en vez de al contrato -ver docstrings en generate_infra.py::build_worker).
def test_puerto_del_contrato_llega_como_env_port():
    """ANTES viajaba como 'VLLM_PORT', que ningún entrypoint.sh leía; los tres
    leen 'PORT' -el puerto real quedaba acoplado al default de la imagen."""
    cfg = load_base_config()
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["PORT"] == str(cfg["workloads"][0]["puerto"])
    assert "VLLM_PORT" not in envs


def test_cantidad_gpus_llega_como_tensor_parallel_size():
    """ANTES SkyPilot pedía la instancia con N GPUs pero WORKER_RUN_SCRIPT
    nunca exportaba TENSOR_PARALLEL_SIZE: se pagaban N GPUs y vLLM usaba 1."""
    cfg = clone(load_base_config())
    cfg["workloads"][0]["cantidad_gpus"] = 2
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["TENSOR_PARALLEL_SIZE"] == "2"


def test_cantidad_gpus_default_es_uno():
    cfg = load_base_config()
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["TENSOR_PARALLEL_SIZE"] == "1"


def test_port_y_tensor_parallel_size_se_exportan_en_worker_run_script():
    cfg = load_base_config()
    run = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["run"]
    assert 'export PORT="${PORT}"' in run
    assert 'export TENSOR_PARALLEL_SIZE="${TENSOR_PARALLEL_SIZE}"' in run


def test_runtime_vllm_ausente_no_agrega_envs():
    """Sin 'runtime_vllm' en el workload (contrato actual), no debe aparecer
    ninguna de sus env vars -el compose usa su propio default."""
    cfg = load_base_config()
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    for var in ("DTYPE", "KV_CACHE_DTYPE", "ENFORCE_EAGER", "MAMBA_SSM_CACHE_DTYPE",
                "VLLM_ATTENTION_BACKEND"):
        assert var not in envs


def test_runtime_vllm_dtype_se_propaga():
    """Caso real: forzar DTYPE=half en una GPU Turing (T4), que no soporta
    bfloat16 -ver el veredicto de compatibilidad T4 en el plan."""
    cfg = clone(load_base_config())
    cfg["workloads"][0]["runtime_vllm"] = {"dtype": "half"}
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["DTYPE"] == "half"
    assert "KV_CACHE_DTYPE" not in envs


def test_runtime_vllm_todos_los_campos_se_propagan():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["runtime_vllm"] = {
        "dtype": "half",
        "kv_cache_dtype": "auto",
        "enforce_eager": True,
        "mamba_ssm_cache_dtype": "float32",
        "attention_backend": "FLASHINFER",
    }
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["DTYPE"] == "half"
    assert envs["KV_CACHE_DTYPE"] == "auto"
    assert envs["ENFORCE_EAGER"] == "1"
    assert envs["MAMBA_SSM_CACHE_DTYPE"] == "float32"
    assert envs["VLLM_ATTENTION_BACKEND"] == "FLASHINFER"


def test_runtime_vllm_enforce_eager_false_se_propaga_como_cero():
    cfg = clone(load_base_config())
    cfg["workloads"][0]["runtime_vllm"] = {"enforce_eager": False}
    envs = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["envs"]
    assert envs["ENFORCE_EAGER"] == "0"


def test_runtime_vllm_valores_se_exportan_en_worker_run_script():
    cfg = load_base_config()
    run = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])["run"]
    for var in ("DTYPE", "KV_CACHE_DTYPE", "ENFORCE_EAGER", "MAMBA_SSM_CACHE_DTYPE",
                "VLLM_ATTENTION_BACKEND"):
        assert f'export {var}="${{{var}:-}}"' in run


# -- usuario/raíz remota según la nube (scripts/cloud_remote.py) -------------
def test_build_worker_usa_ubuntu_por_defecto_aws():
    cfg = load_base_config()
    assert cfg["red_y_aislamiento"].get("cloud") in (None, "aws")
    worker = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])
    assert any("/home/ubuntu/sooniverse_infra" in dest for dest in worker["file_mounts"])
    assert "sudo usermod -aG docker ubuntu" in worker["setup"]


def test_build_worker_usa_azureuser_en_azure():
    """ANTES el repo asumía 'ubuntu' sin importar la nube (comentario falso en
    generate_infra.py, heredado de la rama Azure) -verificado como incorrecto
    contra sky/templates/azure-ray.yml.j2."""
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    worker = TopologyBuilder(cfg).build_worker(cfg["workloads"][0])
    assert any("/home/azureuser/sooniverse_infra" in dest for dest in worker["file_mounts"])
    assert "/home/ubuntu/" not in str(worker["file_mounts"])
    assert "sudo usermod -aG docker azureuser" in worker["setup"]
    assert "sudo usermod -aG docker ubuntu" not in worker["setup"]


def test_build_gateway_usa_azureuser_en_azure():
    cfg = clone(load_base_config())
    cfg["red_y_aislamiento"]["cloud"] = "azure"
    gateway = TopologyBuilder(cfg).build_gateway()
    assert any("/home/azureuser/sooniverse_infra" in dest for dest in gateway["file_mounts"])
    assert "sudo usermod -aG docker azureuser" in gateway["setup"]


# -- fix del bug de login del admin en el primer OneShot ---------------------
# (ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS calculados con la IP efimera vieja en
# vez de la IP reservada -ver GATEWAY_RESERVED_IP en generate_infra.py).
def test_build_gateway_expone_gateway_reserved_ip_cuando_hay_dominio():
    class FakeNetworkOutputs:
        gateway_eip_public_ip = "203.0.113.10"

    cfg = clone(load_base_config())
    assert cfg["gateway"]["dominio"]["habilitado"] is True
    builder = TopologyBuilder(cfg)
    builder.apply_network_outputs(FakeNetworkOutputs())
    envs = builder.build_gateway()["envs"]
    assert envs["GATEWAY_RESERVED_IP"] == "203.0.113.10"


def test_build_gateway_reserved_ip_vacia_sin_network_outputs():
    """'gestion_red: existente' (o cualquier corrida sin AwsNetworkManager/
    AzureNetworkManager) nunca puebla _network_outputs -no debe reventar,
    solo caer al valor vacío (el script usa ifconfig.me de respaldo)."""
    cfg = clone(load_base_config())
    builder = TopologyBuilder(cfg)  # sin apply_network_outputs()
    envs = builder.build_gateway()["envs"]
    assert envs["GATEWAY_RESERVED_IP"] == ""


def test_gateway_run_script_prefiere_la_ip_reservada_sobre_ifconfig_me():
    cfg = clone(load_base_config())
    builder = TopologyBuilder(cfg)
    run_script = builder.build_gateway()["run"]
    assert 'PUBLIC_IP_PRE="${GATEWAY_RESERVED_IP:-$(curl -s --max-time 5 ifconfig.me || true)}"' in run_script


def test_preserve_keys_incluye_allowed_hosts_y_csrf():
    """Sin esto, cualquier cosa que recree los contenedores del Gateway
    despues de asociar la IP (sync_openwebui_models.py, un reinicio, un
    '--only gateway' posterior) borraba ALLOWED_HOSTS/CSRF_TRUSTED_ORIGINS
    del .env remoto y rompia todo POST del panel con HTTPS real."""
    import inspect

    import generate_infra

    fuente = inspect.getsource(generate_infra._associate_gateway_eip)
    assert '"ALLOWED_HOSTS"' in fuente
    assert '"CSRF_TRUSTED_ORIGINS"' in fuente
    assert '"HTTPS_ACTIVO"' in fuente
    assert '"CHAT_URL"' in fuente
    assert '"SOONIVERSE_PANEL_URL"' in fuente
