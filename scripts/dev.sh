#!/usr/bin/env bash
docker compose --env-file .env.dev --project-name guaia-instagram-collector-dev "$@"
