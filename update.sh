#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"
git pull
docker compose up -d --build
docker compose ps
