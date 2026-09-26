"""
Pruebas de las comprobaciones de verify_deployment.py que antes solo miraban
workloads[0] -con dos (o más) modelos desplegados, un segundo modelo roto
pasaba la verificación igual. Cloud-agnóstico: no requiere AWS/Azure real.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import verify_deployment as vd  # noqa: E402


def _config(*workloads):
    return {
        "cliente": {"id": "acme", "entorno": "prod"},
        "workloads": list(workloads),
    }


def _wl(id_, puerto=8007, nombre_publico=None, tipo_tarea="llm-texto"):
    wl = {"id": id_, "puerto": puerto, "tipo_tarea": tipo_tarea}
    if nombre_publico:
        wl["nombre_publico"] = nombre_publico
    return wl


# -- check_end_to_end_completion ---------------------------------------------
def test_end_to_end_prueba_todos_los_modelos_no_solo_el_primero(monkeypatch):
    """Bug real: solo se probaba workloads[0]. Con dos modelos, un segundo
    modelo roto debía hacer FAIL a esta comprobación."""
    cfg = _config(
        _wl("qwen3-5-llm", nombre_publico="sooniverse-qwen3.5"),
        _wl("nemotron-llm", puerto=8008, nombre_publico="sooniverse-nemotron"),
    )
    ctx = vd.VerificationContext(config=cfg)
    monkeypatch.setattr(vd.VerificationContext, "base_url", property(lambda self: "http://1.2.3.4"))

    llamados = []

    def fake_post(url, payload, headers=None, timeout=20):
        llamados.append(payload["model"])
        if payload["model"] == "sooniverse-nemotron":
            return {"json": {"error": "modelo roto"}}
        return {"json": {"choices": [{"message": {"content": "pong"}}]}}

    monkeypatch.setattr(vd, "_http_post", fake_post)

    result = vd.check_end_to_end_completion(ctx)

    assert set(llamados) == {"sooniverse-qwen3.5", "sooniverse-nemotron"}
    assert result.status == "FAIL"
    assert "sooniverse-nemotron" in result.detail


def test_end_to_end_ok_cuando_todos_los_modelos_responden(monkeypatch):
    cfg = _config(
        _wl("qwen3-5-llm", nombre_publico="sooniverse-qwen3.5"),
        _wl("nemotron-llm", puerto=8008, nombre_publico="sooniverse-nemotron"),
    )
    ctx = vd.VerificationContext(config=cfg)
    monkeypatch.setattr(vd.VerificationContext, "base_url", property(lambda self: "http://1.2.3.4"))
    monkeypatch.setattr(
        vd, "_http_post",
        lambda url, payload, headers=None, timeout=20: {"json": {"choices": [{"message": {"content": "pong"}}]}},
    )

    result = vd.check_end_to_end_completion(ctx)

    assert result.status == "OK"


def test_end_to_end_ignora_workloads_de_embeddings(monkeypatch):
    """Un modelo de embeddings no expone /v1/chat/completions -no debe
    intentarse ni contar como fallo."""
    cfg = _config(
        _wl("qwen3-5-llm", nombre_publico="sooniverse-qwen3.5"),
        _wl("gte-qwen2-embed", puerto=8009, nombre_publico="sooniverse-embeddings", tipo_tarea="embeddings"),
    )
    ctx = vd.VerificationContext(config=cfg)
    monkeypatch.setattr(vd.VerificationContext, "base_url", property(lambda self: "http://1.2.3.4"))

    llamados = []

    def fake_post(url, payload, headers=None, timeout=20):
        llamados.append(payload["model"])
        return {"json": {"choices": [{"message": {"content": "pong"}}]}}

    monkeypatch.setattr(vd, "_http_post", fake_post)

    result = vd.check_end_to_end_completion(ctx)

    assert llamados == ["sooniverse-qwen3.5"]
    assert result.status == "OK"


# -- check_worker_has_internet_egress ----------------------------------------
def test_worker_egress_revisa_todos_los_clusters_aprovisionados(monkeypatch):
    """Bug real: solo se revisaba el clúster de workloads[0]. Con dos
    workloads en clústeres distintos, un segundo clúster sin NAT pasaba
    igual."""
    cfg = _config(_wl("qwen3-5-llm"), _wl("nemotron-llm", puerto=8008))
    ctx = vd.VerificationContext(config=cfg, sky_available=True)

    revisados = []

    def fake_cluster_is_up(cluster):
        return True

    def fake_exec(cluster, remote_cmd, timeout=30, retries=2):
        revisados.append(cluster)
        if "nemotron" in cluster:
            return "SOONIVERSE_CURL_FAIL"
        return "SOONIVERSE_CURL_OK"

    monkeypatch.setattr(vd, "_cluster_is_up", fake_cluster_is_up)
    monkeypatch.setattr(vd, "_sky_exec_remote_output", fake_exec)

    result = vd.check_worker_has_internet_egress(ctx)

    assert len(revisados) == 2
    assert result.status == "FAIL"
    assert "nemotron" in result.detail


def test_worker_egress_ok_cuando_todos_los_clusters_tienen_nat(monkeypatch):
    cfg = _config(_wl("qwen3-5-llm"), _wl("nemotron-llm", puerto=8008))
    ctx = vd.VerificationContext(config=cfg, sky_available=True)

    monkeypatch.setattr(vd, "_cluster_is_up", lambda cluster: True)
    monkeypatch.setattr(vd, "_sky_exec_remote_output", lambda *a, **k: "SOONIVERSE_CURL_OK")

    result = vd.check_worker_has_internet_egress(ctx)

    assert result.status == "OK"


def test_worker_egress_na_cuando_ningun_cluster_esta_arriba(monkeypatch):
    cfg = _config(_wl("qwen3-5-llm"), _wl("nemotron-llm", puerto=8008))
    ctx = vd.VerificationContext(config=cfg, sky_available=True)

    monkeypatch.setattr(vd, "_cluster_is_up", lambda cluster: False)

    result = vd.check_worker_has_internet_egress(ctx)

    assert result.status == "N/A"
