#!/usr/bin/env bash
docker compose --env-file .env.hom --project-name guaia-instagram-collector-hom "$@"
