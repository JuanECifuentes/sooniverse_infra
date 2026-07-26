# ==============================================================================
# SOONIVERSE :: Panel de Métricas y API Keys (Django)
# ==============================================================================
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=sooniverse_panel.settings

WORKDIR /app

RUN apt-get update \
 && apt-get install -y --no-install-recommends libpq5 curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh \
 && mkdir -p /app/staticfiles

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=5 \
  CMD curl -fsS http://localhost:8000/healthz/ || exit 1

ENTRYPOINT ["./entrypoint.sh"]
