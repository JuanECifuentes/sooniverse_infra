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
