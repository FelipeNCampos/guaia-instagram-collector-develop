# ---------------------------------------------------------------------------
# Imagem PRODUÇÃO Selenium Chrome (tag latest estável).
# selenium/standalone-chrome:latest = Chrome 120+ + ChromeDriver compatível.
# ---------------------------------------------------------------------------
FROM selenium/standalone-chrome:latest

LABEL maintainer="instagram-collector" \
      description="Instagram post collector (Selenium + PostgreSQL + GuaIA)"

# Configurações root
USER root

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    CHROME_IN_DOCKER=1

# Shared memory CRÍTICO para Chrome
RUN mkdir -p /dev/shm && \
    chmod 1777 /dev/shm

# Python + dependências
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv ca-certificates curl && \
    rm -rf /var/lib/apt/lists/* && \
    ln -sf /usr/bin/python3 /usr/local/bin/python && \
    ln -sf /usr/bin/pip3 /usr/local/bin/pip

WORKDIR /app

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Código + docker-entrypoint.sh (sobe supervisord antes do app)
COPY . .

# Permissões
RUN mkdir -p /app/data /app/output && \
    chown -R seluser:seluser /app && \
    chmod -R 755 /app/data /app/output && \
    chmod +x /app/docker-entrypoint.sh

USER seluser
ENV HEADLESS=true

HEALTHCHECK --interval=30s --timeout=10s --start-period=150s --retries=3 \
    CMD curl -f http://localhost:4444/wd/hub/status || curl -sf http://127.0.0.1:4444/status || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["python", "instagram_api_feeder.py"]