#!/usr/bin/env bash
docker compose --env-file .env.prod --project-name guaia-instagram-collector-prod "$@"
