"""
Pruebas de la clasificación de resultados en scripts/test_model_capabilities.py
(soportado / no soportado / no concluyente). Los HTTP reales se simulan
monkeypatcheando _http_post; no requiere AWS ni un despliegue real.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import test_model_capabilities as tmc  # noqa: E402


def test_vision_supported_on_200_with_choices(monkeypatch):
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 200, "json": {"choices": [{}]}})
    result = tmc.probe_vision("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is True


def test_vision_unsupported_on_known_error(monkeypatch):
    error_body = {"error": {"message": "This model does not support multimodal image input"}}
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 400, "json": error_body})
    result = tmc.probe_vision("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is False


def test_vision_inconclusive_on_unrelated_error(monkeypatch):
    error_body = {"error": {"message": "Internal server error, please retry"}}
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 500, "json": error_body})
    result = tmc.probe_vision("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is None


def test_vision_inconclusive_on_5xx_even_with_marker_in_message(monkeypatch):
    """Endurecimiento: un 5xx transitorio que mencione 'image' de casualidad ya
    NO se clasifica como 'no soportado' -solo un 4xx (error del cliente) cuenta."""
    error_body = {"error": {"message": "upstream timeout while decoding image"}}
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 503, "json": error_body})
    result = tmc.probe_vision("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is None


def test_tool_calling_unsupported_on_real_vllm_error(monkeypatch):
    # Mensaje real observado: litellm.BadRequestError - "auto" tool choice
    # requires --enable-auto-tool-choice and --tool-call-parser to be set.
    error_body = {
        "error": {
            "message": '"auto" tool choice requires --enable-auto-tool-choice '
                       "and --tool-call-parser to be set"
        }
    }
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 400, "json": error_body})
    result = tmc.probe_tool_calling("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is False


def test_tool_calling_supported_on_200(monkeypatch):
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 200, "json": {"choices": [{}]}})
    result = tmc.probe_tool_calling("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is True


def test_json_object_supported_on_200(monkeypatch):
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 200, "json": {"choices": [{}]}})
    result = tmc.probe_json_object("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is True


def test_json_object_unsupported_on_grammar_error(monkeypatch):
    # Mensaje real documentado contra vLLM: falta el backend de grammar (xgrammar).
    error_body = {"error": {"message": "compile_grammar_error: No module named 'xgrammar'"}}
    monkeypatch.setattr(tmc, "_http_post", lambda *a, **k: {"status": 400, "json": error_body})
    result = tmc.probe_json_object("1.2.3.4", "sooniverse-qwen3.5", {})
    assert result.supported is False


def test_retry_stops_immediately_on_conclusive_result(monkeypatch):
    calls = []

    def fake_probe(*a, **k):
        calls.append(1)
        return tmc.ProbeResult(True, "ok")

    result, attempts = tmc._probe_with_retry(fake_probe, "1.2.3.4", "m", {}, attempts=3, backoff=(0, 0, 0))
    assert result.supported is True
    assert attempts == 1
    assert len(calls) == 1


def test_retry_keeps_trying_on_inconclusive_then_succeeds(monkeypatch):
    calls = []

    def fake_probe(*a, **k):
        calls.append(1)
        if len(calls) < 3:
            return tmc.ProbeResult(None, "timeout")
        return tmc.ProbeResult(True, "ok al tercer intento")

    result, attempts = tmc._probe_with_retry(fake_probe, "1.2.3.4", "m", {}, attempts=3, backoff=(0, 0, 0))
    assert result.supported is True
    assert attempts == 3
    assert len(calls) == 3


def test_retry_fail_closed_when_always_inconclusive(monkeypatch):
    """Fail-closed: si los 3 intentos son inconclusos, el resultado final sigue
    siendo None (nunca se 'convierte' en True por agotar reintentos)."""

    def fake_probe(*a, **k):
        return tmc.ProbeResult(None, "timeout persistente")

    result, attempts = tmc._probe_with_retry(fake_probe, "1.2.3.4", "m", {}, attempts=3, backoff=(0, 0, 0))
    assert result.supported is None
    assert attempts == 3


def test_main_reports_failure_on_dangerous_mismatch(monkeypatch, tmp_path, capsys):
    """declarado tool_calling: true pero el modelo lo rechaza -> exit code 1."""
    config_path = REPO_ROOT / "config_global.yaml"

    monkeypatch.setattr(tmc, "discover_gateway_ip", lambda cliente: "1.2.3.4")
    monkeypatch.setattr(tmc, "_read_env_var", lambda key: "sk-test")
    monkeypatch.setattr(tmc, "probe_vision", lambda *a, **k: tmc.ProbeResult(True, "ok"))
    monkeypatch.setattr(
        tmc, "probe_tool_calling",
        lambda *a, **k: tmc.ProbeResult(False, "auto tool choice requires --enable-auto-tool-choice"),
    )
    monkeypatch.setattr(tmc, "probe_json_object", lambda *a, **k: tmc.ProbeResult(True, "ok"))
    monkeypatch.setattr(tmc, "probe_streaming", lambda *a, **k: tmc.ProbeResult(True, "ok"))

    import yaml
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["workloads"][0]["capacidades"]["tool_calling"] = True
    cfg["workloads"][0]["capacidades"]["tool_call_parser"] = "hermes"
    patched_path = tmp_path / "config_global.yaml"
    with patched_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)

    monkeypatch.setattr(sys, "argv", ["test_model_capabilities.py", "--config", str(patched_path)])
    exit_code = tmc.main()
    assert exit_code == 1
    assert "declaraste 'true' pero el modelo lo rechazó" in capsys.readouterr().out


def test_main_writes_json_summary_with_effective_capabilities(monkeypatch, tmp_path, capsys):
    """effective_* en el --json de salida refleja la política fail-closed:
    declarado Y probado=True."""
    config_path = REPO_ROOT / "config_global.yaml"

    monkeypatch.setattr(tmc, "discover_gateway_ip", lambda cliente: "1.2.3.4")
    monkeypatch.setattr(tmc, "_read_env_var", lambda key: "sk-test")
    monkeypatch.setattr(tmc, "probe_vision", lambda *a, **k: tmc.ProbeResult(None, "timeout"))
    monkeypatch.setattr(tmc, "probe_tool_calling", lambda *a, **k: tmc.ProbeResult(False, "no soportado"))
    monkeypatch.setattr(tmc, "probe_json_object", lambda *a, **k: tmc.ProbeResult(True, "ok"))
    monkeypatch.setattr(tmc, "probe_streaming", lambda *a, **k: tmc.ProbeResult(True, "ok"))

    import yaml
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["workloads"][0]["capacidades"]["vision"] = True
    patched_path = tmp_path / "config_global.yaml"
    with patched_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(cfg, f)

    json_out = tmp_path / "caps.json"
    monkeypatch.setattr(
        sys, "argv",
        ["test_model_capabilities.py", "--config", str(patched_path), "--json", str(json_out)],
    )
    tmc.main()

    import json
    payload = json.loads(json_out.read_text(encoding="utf-8"))
    model = payload["models"][0]
    # declared_vision=True pero probed_vision=None (inconcluso) -> fail-closed -> False
    assert model["effective_vision"] is False
    assert model["effective_tool_calling"] is False
    assert model["effective_json_object"] is True
