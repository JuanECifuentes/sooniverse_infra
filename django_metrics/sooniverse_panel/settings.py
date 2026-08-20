"""
==============================================================================
Sooniverse Panel :: Configuración Django (Métricas + API Keys)
==============================================================================
Aplicación ligera. No define modelos de negocio propios: lee las tablas creadas
por `database/init_schema.sql` en el esquema PostgreSQL `sooniverse`.
"""

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = BASE_DIR.parent


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def _bool(key: str, default: bool = False) -> bool:
    return _env(key, str(default)).strip().lower() in ("1", "true", "yes", "on")


# -----------------------------------------------------------------------------
# Carga de .env del repo cuando se ejecuta fuera de Docker (desarrollo local).
# El archivo es autoritativo, igual que en scripts/db_setup.py: un shell con
# credenciales obsoletas no debe secuestrar la conexión. Dentro del contenedor
# no existe `/.env`, así que manda el entorno inyectado por docker compose.
# -----------------------------------------------------------------------------
_ENV_FILE = REPO_ROOT / ".env"
if _ENV_FILE.exists():
    for _raw in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        _line = _raw.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ[_k.strip()] = _v.strip().strip('"').strip("'")

# -----------------------------------------------------------------------------
# Núcleo
# -----------------------------------------------------------------------------
SECRET_KEY = _env("SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = _bool("DEBUG", False)
ALLOWED_HOSTS = [h.strip() for h in _env("ALLOWED_HOSTS", "*").split(",") if h.strip()]
CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in _env("CSRF_TRUSTED_ORIGINS", "").split(",") if o.strip()
]

# nginx antepone /panel (ver docker_images/gateway/nginx/default.conf,
# location /panel/ -> X-Script-Name /panel) para que Django genere URLs y
# enlaces estáticos con ese prefijo. Vacío en desarrollo local sin proxy.
FORCE_SCRIPT_NAME = _env("FORCE_SCRIPT_NAME", "") or None

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.humanize",
    "metrics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "sooniverse_panel.urls"
WSGI_APPLICATION = "sooniverse_panel.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "metrics.context_processors.branding",
            ],
        },
    },
]

# -----------------------------------------------------------------------------
# Base de datos (la misma que usa LiteLLM para Spend/Usage)
# -----------------------------------------------------------------------------
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": _env("DB_NAME", "sooniverse"),
        "USER": _env("DB_USER", "postgres"),
        "PASSWORD": _env("DB_PASSWORD", ""),
        "HOST": _env("DB_HOST", "localhost"),
        "PORT": _env("DB_PORT", "5432"),
        "CONN_MAX_AGE": 60,
        "OPTIONS": {
            # Permite referirse a las tablas del esquema aislado sin cualificar.
            "options": "-c search_path=sooniverse",
            "connect_timeout": 10,
        },
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
]

# -----------------------------------------------------------------------------
# i18n / estáticos
# -----------------------------------------------------------------------------
LANGUAGE_CODE = "es"
TIME_ZONE = _env("TIME_ZONE", "America/Bogota")
USE_I18N = True
USE_TZ = True

# Detrás de nginx, /panel/static/ lo sirve nginx directamente (alias al volumen
# panel_static, ver docker-compose.yml) desde los archivos que collectstatic
# escribe aquí con nombres con hash (CompressedManifestStaticFilesStorage);
# Django no interviene en esa ruta. STATIC_URL debe incluir el mismo prefijo
# que FORCE_SCRIPT_NAME para que {% static %} genere el enlace correcto.
_script_name = _env("FORCE_SCRIPT_NAME", "")
STATIC_URL = f"{_script_name}/static/" if _script_name else "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]
# En producción (DEBUG=False, que es como corre el contenedor) se usa la
# subclase propia con hash + precompresión: el JS del panel usa ES modules y sus
# imports relativos también tienen que reescribirse a los nombres con hash (ver
# storage.py). Con DEBUG=True, `runserver` sirve desde los directorios fuente y
# no sabe resolver un nombre con hash, así que ahí se usa el almacenamiento
# plano; si no, cada archivo estático daría 404 en desarrollo.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": (
            "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
            else "sooniverse_panel.storage.SooniverseStaticFilesStorage"
        )
    },
}

LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/metrics/"

# -----------------------------------------------------------------------------
# Integración con LiteLLM Proxy
# -----------------------------------------------------------------------------
LITELLM_BASE_URL = _env("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/")
LITELLM_MASTER_KEY = _env("LITELLM_MASTER_KEY", "")
LITELLM_TIMEOUT = int(_env("LITELLM_TIMEOUT", "30"))

# Contexto de tenancy heredado del contrato de infraestructura
CLIENTE_ID = _env("CLIENTE_ID", "default")
ENTORNO = _env("ENTORNO", "prod")

# -----------------------------------------------------------------------------
# Analítica de ritmo de uso y capacidad
# -----------------------------------------------------------------------------
# Coste por hora de la infraestructura de inferencia (la GPU, no el gasto en
# tokens). Es lo que permite responder "¿cuánto me cuesta la máquina parada?"
# en la tarjeta de tiempos muertos. Con 0, la tarjeta oculta la columna de coste
# en vez de mostrar $0,0000 en todas las filas.
# Referencia us-east-1: g6.xlarge ~0.80 + t3.large ~0.08 + NAT/EIP ~0.05.
METRICS_COSTE_HORA_USD = float(_env("METRICS_COSTE_HORA_USD", "0") or 0)

# Refresco automático de métricas (segundos). 0 desactiva el job.
METRICS_REFRESH_INTERVAL = int(_env("METRICS_REFRESH_INTERVAL", "300"))

# Ventana por defecto del panel (días)
METRICS_DEFAULT_WINDOW_DAYS = int(_env("METRICS_DEFAULT_WINDOW_DAYS", "30"))

# -----------------------------------------------------------------------------
# Seguridad (el Gateway es la única superficie pública)
# -----------------------------------------------------------------------------
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True
USE_X_FORWARDED_HOST = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": _env("LOG_LEVEL", "INFO")},
}
