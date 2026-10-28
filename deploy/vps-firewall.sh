#!/bin/sh
# Restrict the unauthenticated geo APIs without changing SSH or hosting-panel rules.
set -eu
interface=$(ip -4 route show default | awk 'NR==1 {print $5}')
iptables -N OSTROVOK-GEO 2>/dev/null || true
iptables -F OSTROVOK-GEO
iptables -A OSTROVOK-GEO -s 46.242.15.146/32 -j RETURN
iptables -A OSTROVOK-GEO -s 23.95.85.12/32 -j RETURN
iptables -A OSTROVOK-GEO -j DROP
for port in 8080 8081; do
    iptables -C DOCKER-USER -i "$interface" -p tcp -m conntrack --ctorigdstport "$port" -j OSTROVOK-GEO 2>/dev/null ||
        iptables -I DOCKER-USER 1 -i "$interface" -p tcp -m conntrack --ctorigdstport "$port" -j OSTROVOK-GEO
done
