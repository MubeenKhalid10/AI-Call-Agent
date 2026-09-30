#!/usr/bin/env bash
# One-time (and safely repeatable) setup of an AWS EC2 Ubuntu 24.04 host for the
# AI Call Agent. Run it ON THE INSTANCE as the `ubuntu` user:
#
#   git clone https://github.com/MubeenKhalid10/AI-Call-Agent.git ~/AI-Call-Agent
#   bash ~/AI-Call-Agent/deploy/aws/setup-ec2.sh <YOUR_HOSTNAME> <EMAIL_FOR_LETS_ENCRYPT>
#
# What it does, in order (each step is idempotent):
#   1. apt packages: git, curl, ca-certificates, nginx, certbot
#   2. a 2 GB swap file (headroom on a 2 GB instance; two Python processes with
#      the voice stack commit ~1.2 GB before a call starts)
#   3. uv (the project's package manager) and Python 3.12 (server/.python-version)
#   4. the project's environment: `uv sync --frozen --all-extras --no-dev` in server/
#   5. nginx + a Let's Encrypt certificate for the hostname (certbot --webroot;
#      the apt package's certbot.timer renews it, and the hook reloads nginx)
#   6. the two systemd units, enabled but NOT started
#
# It does NOT start the two application services, because they need server/.env,
# which is copied from the PC afterwards (README.md). It never touches .env,
# the database, or any application file. The EC2 security group is the firewall
# (22 from your IP, 80 and 443 from anywhere); Ubuntu's own ufw stays off.
set -euo pipefail

HOSTNAME_ARG="${1:-}"
EMAIL_ARG="${2:-}"
if [[ -z "$HOSTNAME_ARG" || "$HOSTNAME_ARG" == *"<"* || -z "$EMAIL_ARG" || "$EMAIL_ARG" != *"@"* ]]; then
    echo "usage: bash $0 <YOUR_HOSTNAME> <EMAIL_FOR_LETS_ENCRYPT>" >&2
    exit 2
fi
if [[ "$(id -un)" != "ubuntu" ]]; then
    echo "run this as the ubuntu user (it installs into /home/ubuntu)" >&2
    exit 2
fi

REPO_URL="https://github.com/MubeenKhalid10/AI-Call-Agent.git"
REPO_DIR="$HOME/AI-Call-Agent"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SITE=/etc/nginx/sites-available/ai-agent-voice
WEBROOT=/var/www/certbot

echo "== 1/6 apt packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git curl ca-certificates nginx certbot

echo "== 2/6 swap"
if ! sudo swapon --show | grep -q /swapfile; then
    if [[ ! -f /swapfile ]]; then
        sudo fallocate -l 2G /swapfile
        sudo chmod 600 /swapfile
        sudo mkswap /swapfile >/dev/null
    fi
    sudo swapon /swapfile
    grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi
# Swap is the safety net, not working memory: keep the voice path in RAM.
echo 'vm.swappiness=10' | sudo tee /etc/sysctl.d/60-ai-agent-swappiness.conf >/dev/null
sudo sysctl -q -p /etc/sysctl.d/60-ai-agent-swappiness.conf
free -m | sed -n 1,3p

echo "== 3/6 uv + Python 3.12"
if ! command -v "$HOME/.local/bin/uv" >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.12

echo "== 4/6 project environment"
if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR/server"
# The lock is the source of truth; the `voice` extra is the bot (README, "The voice extra").
uv sync --frozen --all-extras --no-dev
uv run --no-sync python -c "import pipecat, fastembed, aiortc; print('pipecat', pipecat.__version__, 'ok')"

echo "== 5/6 nginx + certificate"
sudo mkdir -p "$WEBROOT"
sudo rm -f /etc/nginx/sites-enabled/default
sudo systemctl reload nginx 2>/dev/null || true   # stop serving the distribution's welcome page
# Let's Encrypt validates over DNS -> this host. Without the A record the request
# only burns the rate limit (five failures per hour), so it is not attempted.
PUBLIC_IP="$(curl -s --max-time 5 https://checkip.amazonaws.com || true)"
RESOLVED="$(getent ahostsv4 "$HOSTNAME_ARG" | awk '{print $1}' | head -1 || true)"
CERT="/etc/letsencrypt/live/$HOSTNAME_ARG/fullchain.pem"
if ! sudo test -f "$CERT" && [[ -z "$RESOLVED" || "$RESOLVED" != "$PUBLIC_IP" ]]; then
    echo "   DNS: $HOSTNAME_ARG -> ${RESOLVED:-nothing}; this host is ${PUBLIC_IP:-unknown}."
    echo "   SKIPPED the certificate and the HTTPS site. Create the A record, then run this script again."
    SKIP_TLS=1
fi
if [[ -z "${SKIP_TLS:-}" ]]; then
if ! sudo test -f "$CERT"; then
    # Stage 1: plain HTTP, only the ACME challenge, so certbot can prove the hostname.
    sudo tee "$SITE" >/dev/null <<EOF
server {
    listen 80;
    listen [::]:80;
    server_name $HOSTNAME_ARG;
    server_tokens off;
    location ^~ /.well-known/acme-challenge/ { root $WEBROOT; default_type text/plain; }
    location / { return 404; }
}
EOF
    sudo ln -sf "$SITE" /etc/nginx/sites-enabled/ai-agent-voice
    sudo nginx -t
    sudo systemctl enable --now nginx
    sudo systemctl reload nginx
    sudo certbot certonly --webroot -w "$WEBROOT" -d "$HOSTNAME_ARG" \
        --non-interactive --agree-tos -m "$EMAIL_ARG" --no-eff-email \
        --deploy-hook "systemctl reload nginx"
fi
# Stage 2: the real site (HTTPS + websocket proxy to the bot on loopback).
sed "s#<YOUR_HOSTNAME>#$HOSTNAME_ARG#g" "$HERE/nginx-voice.conf" | sudo tee "$SITE" >/dev/null
sudo ln -sf "$SITE" /etc/nginx/sites-enabled/ai-agent-voice
sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx
systemctl is-enabled certbot.timer || echo "WARNING: certbot.timer is not enabled; renewals will not run (sudo systemctl enable --now certbot.timer)"
fi

echo "== 6/6 systemd units"
for unit in ai-agent-bot ai-agent-app; do
    sed -e "s#/home/ubuntu#$HOME#g" -e "s#<YOUR_HOSTNAME>#$HOSTNAME_ARG#g" "$HERE/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable ai-agent-bot ai-agent-app

echo
if [[ -n "${SKIP_TLS:-}" ]]; then
    echo "Setup done EXCEPT HTTPS: point $HOSTNAME_ARG at ${PUBLIC_IP:-this host} and run the script again."
else
    echo "Setup done. https://$HOSTNAME_ARG answers 502 until the bot is started."
fi
if [[ ! -f "$REPO_DIR/server/.env" ]]; then
    echo "Next: copy server/.env from the PC (README.md), set TELEPHONY_PUBLIC_URL=https://$HOSTNAME_ARG, then:"
    echo "  sudo systemctl start ai-agent-bot ai-agent-app"
fi
