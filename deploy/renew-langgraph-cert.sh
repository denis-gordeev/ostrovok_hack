#!/bin/sh
set -eu
/usr/bin/docker run --rm \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/letsencrypt:/var/www/letsencrypt \
  certbot/certbot:v5.4.0 renew --quiet
cd /opt/ostrovok
/usr/bin/docker compose -f docker-compose.yml -f deploy/compose.langgraph.yml \
  exec -T langgraph-gateway nginx -s reload
