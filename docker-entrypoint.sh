#!/usr/bin/env bash
# A imagem selenium/standalone-chrome depende do supervisord (Xvfb, etc.).
# Substituir só o CMD por "python ..." impede o entry_point oficial e o Chrome morre ao subir.
set -euo pipefail

if [[ -f /.dockerenv ]] && [[ "${CHROME_IN_DOCKER:-0}" =~ ^(1|true|yes|on)$ ]]; then
  if [[ -f /etc/supervisord.conf ]]; then
    if [[ -n "${VIRTUAL_ENV:-}" ]]; then
      # shellcheck disable=SC1090
      source "${VIRTUAL_ENV}/bin/activate"
    fi
    echo "[instagram-entrypoint] Iniciando supervisord (Xvfb + stack Selenium)..."
    supervisord --configuration /etc/supervisord.conf &
    _sup_pid=$!
    echo "[instagram-entrypoint] Aguardando Grid em http://127.0.0.1:4444 ..."
    _ready=0
    for _ in $(seq 1 120); do
      if curl -sf "http://127.0.0.1:4444/status" >/dev/null 2>&1; then
        _ready=1
        break
      fi
      if curl -sf "http://127.0.0.1:4444/wd/hub/status" >/dev/null 2>&1; then
        _ready=1
        break
      fi
      sleep 1
    done
    if [[ "${_ready}" -ne 1 ]]; then
      echo "[instagram-entrypoint] AVISO: Grid não respondeu em 120s; seguindo mesmo assim."
    fi
    sleep 2
  fi
fi

exec "$@"
