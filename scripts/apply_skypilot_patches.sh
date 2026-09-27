#!/usr/bin/env bash
# ==============================================================================
# SOONIVERSE :: Aplica los parches locales a la librería SkyPilot instalada
# ==============================================================================
# Por qué existe: 'venv/lib/.../sky/provision/azure/instance.py' tiene un
# parche autorizado por el operador (TrustedLaunch + secure_boot_enabled=False,
# ver patches/skypilot/0.13.0-azure-trustedlaunch-no-secureboot.patch) que NO
# vive en el paquete 'skypilot' de PyPI. Un 'pip install --force-reinstall',
# un venv nuevo, o un simple 'pip install -U skypilot' lo borra sin avisar, y
# el worker T4 vuelve a colgarse en el prompt interactivo de MOK de Secure
# Boot (ver docstring de scripts/azure_worker_egress_ip.py y el commit que
# introdujo este parche). Este script deja el parche versionado en git y
# reaplicable en un comando, e idempotente: correrlo dos veces no falla.
#
# Uso:
#   scripts/apply_skypilot_patches.sh            # aplica sobre el venv activo
#   VENV_PYTHON=/otra/ruta/venv/bin/python scripts/apply_skypilot_patches.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PATCH_DIR="${REPO_ROOT}/patches/skypilot"
EXPECTED_VERSION="0.13.0"
PYTHON_BIN="${VENV_PYTHON:-python}"

if ! command -v "${PYTHON_BIN}" >/dev/null 2>&1; then
    echo "[skypilot-patch] ERROR: no se encontró el intérprete '${PYTHON_BIN}'." >&2
    echo "[skypilot-patch]        Activa el venv (source venv/bin/activate) antes de correr esto." >&2
    exit 1
fi

INSTALLED_VERSION="$("${PYTHON_BIN}" -c 'import sky; print(sky.__version__)' 2>/dev/null || true)"
if [ -z "${INSTALLED_VERSION}" ]; then
    echo "[skypilot-patch] ERROR: no se pudo importar 'sky'. ¿Está instalado skypilot en este entorno?" >&2
    exit 1
fi
if [ "${INSTALLED_VERSION}" != "${EXPECTED_VERSION}" ]; then
    echo "[skypilot-patch] ERROR: skypilot instalado es ${INSTALLED_VERSION}, este parche está" >&2
    echo "[skypilot-patch]        hecho y verificado solo contra ${EXPECTED_VERSION}. Aplicarlo a" >&2
    echo "[skypilot-patch]        otra versión puede fallar en silencio o corromper el archivo." >&2
    echo "[skypilot-patch]        Fija la versión: pip install 'skypilot[aws,azure]==${EXPECTED_VERSION}'" >&2
    exit 1
fi

SKY_PKG_DIR="$("${PYTHON_BIN}" -c 'import os, sky; print(os.path.dirname(os.path.dirname(sky.__file__)))')"
echo "[skypilot-patch] skypilot ${INSTALLED_VERSION} en ${SKY_PKG_DIR}"

APPLIED_ANY=0
shopt -s nullglob
for patch_file in "${PATCH_DIR}"/*.patch; do
    name="$(basename "${patch_file}")"
    # '-N' hace que GNU patch detecte por sí mismo un parche ya aplicado (o
    # invertido) y lo salte. OJO: en esta versión de GNU patch (2.7.6) el
    # caso "ya aplicado" también sale con exit != 0 (igual que un fallo
    # real), así que no se puede distinguir por el código de salida -hay
    # que mirar el mensaje. '-r /dev/null' descarta el .rej que 'patch'
    # escribiría dentro de site-packages en el caso "ya aplicado" (no es
    # un error, solo ruido); en el caso de fallo real el .rej también se
    # descarta a propósito, porque el mensaje de error ya queda impreso.
    output="$(patch -p1 -d "${SKY_PKG_DIR}" -N -r /dev/null < "${patch_file}" 2>&1)" || true
    if echo "${output}" | grep -q 'previously applied'; then
        echo "[skypilot-patch] '${name}' ya estaba aplicado, sin cambios."
    elif echo "${output}" | grep -q 'FAILED'; then
        echo "[skypilot-patch] ERROR: '${name}' no se pudo aplicar (¿el archivo original de sky cambió?)." >&2
        echo "${output}" >&2
        exit 1
    elif echo "${output}" | grep -q '^patching file'; then
        echo "[skypilot-patch] '${name}' aplicado."
        APPLIED_ANY=1
    else
        echo "[skypilot-patch] ERROR: '${name}' no se pudo aplicar (salida inesperada de 'patch')." >&2
        echo "${output}" >&2
        exit 1
    fi
done
shopt -u nullglob

if [ "${APPLIED_ANY}" -eq 1 ]; then
    # El servidor local de la API de SkyPilot (sky.server.server) importa y
    # cachea el módulo de 'sky' UNA vez al arrancar; si ya estaba corriendo
    # con la versión sin parchear, seguirá usándola aunque el archivo en disco
    # cambie. 'sky api stop' lo apaga; el próximo comando 'sky' lo relanza y
    # relee el módulo ya parchado. Best-effort: si no había servidor corriendo,
    # 'sky api stop' no falla el script.
    echo "[skypilot-patch] Reiniciando el servidor local de la API de SkyPilot..."
    SKY_BIN="$(dirname "$(command -v "${PYTHON_BIN}")")/sky"
    if [ ! -x "${SKY_BIN}" ]; then
        SKY_BIN="sky"
    fi
    "${SKY_BIN}" api stop || true
fi

echo "[skypilot-patch] Listo."
