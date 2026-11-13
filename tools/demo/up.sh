#!/bin/sh
# Start an isolated demo Docker engine (docker:dind on 127.0.0.1:23750) with two demo stacks.
# Point Berth at it: Settings → Docker Engine → Other engine address → tcp://127.0.0.1:23750
# Tear down:  sh tools/demo/down.sh
set -e
cd "$(dirname "$0")"
docker run -d --name berth-demo-engine --privileged -e DOCKER_TLS_CERTDIR= -p 127.0.0.1:23750:2375 docker:dind >/dev/null
export DOCKER_HOST=tcp://127.0.0.1:23750
until docker info >/dev/null 2>&1; do sleep 1; done
docker compose -p shop -f shop.compose.yaml up -d
until docker exec shop-db-1 pg_isready -q -h 127.0.0.1 -U postgres 2>/dev/null; do sleep 1; done
docker exec -i shop-db-1 psql -q -U postgres -d shop < shop-seed.sql >/dev/null
docker compose -p blog -f blog.compose.yaml up -d
docker compose -p blog -f blog.compose.yaml stop search
docker build -q -t demo/hello:1.0 -f Dockerfile.hello . >/dev/null
docker run --name hello-once demo/hello:1.0 >/dev/null
docker pull -q alpine:3.20 >/dev/null
docker volume create backups >/dev/null
echo "demo engine ready at tcp://127.0.0.1:23750"
