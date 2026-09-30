#!/usr/bin/env bash
# One-time (and safely repeatable) setup of an Oracle Cloud Ubuntu VM for the
# AI Call Agent. Run it ON THE VM as the `ubuntu` user:
#
#   git clone https://github.com/MubeenKhalid10/AI-Call-Agent.git ~/AI-Call-Agent
#   bash ~/AI-Call-Agent/deploy/oracle/setup-vm.sh <YOUR_HOSTNAME>.duckdns.org
#
# What it does, in order (each step is idempotent):
#   1. apt packages: git, curl, ca-certificates, iptables-persistent
#   2. uv (the project's package manager) and Python 3.12 (server/.python-version)
#   3. Caddy from its official apt repository
#   4. the VM's own firewall: Oracle's Ubuntu image rejects everything but SSH
#      in iptables, independently of the cloud security list; 80 and 443 are opened
#   5. the project's environment: `uv sync --frozen --all-extras --no-dev` in server/
#   6. the Caddyfile with your hostname, and the two systemd units
#
# It does NOT start the two application services, because they need server/.env,
# which you copy from your PC afterwards (see README.md, step 9). It never
# touches .env, the database, or any application file.
set -euo pipefail

HOSTNAME_ARG="${1:-}"
if [[ -z "$HOSTNAME_ARG" || "$HOSTNAME_ARG" == *"<"* ]]; then
    echo "usage: bash $0 <YOUR_HOSTNAME>.duckdns.org" >&2
    exit 2
fi
if [[ "$(id -un)" != "ubuntu" ]]; then
    echo "run this as the ubuntu user (it installs into /home/ubuntu)" >&2
    exit 2
fi

REPO_URL="https://github.com/MubeenKhalid10/AI-Call-Agent.git"
REPO_DIR="$HOME/AI-Call-Agent"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "== 1/6 apt packages"
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    git curl ca-certificates gnupg debian-keyring debian-archive-keyring apt-transport-https \
    iptables-persistent

echo "== 2/6 uv + Python 3.12"
if ! command -v "$HOME/.local/bin/uv" >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
fi
export PATH="$HOME/.local/bin:$PATH"
uv --version
uv python install 3.12

echo "== 3/6 Caddy"
if ! command -v caddy >/dev/null 2>&1; then
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | sudo gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        | sudo tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    sudo chmod o+r /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
    sudo apt-get update -qq
    sudo apt-get install -y -qq caddy
fi
caddy version

echo "== 4/6 VM firewall (iptables): allow 80 and 443 in addition to 22"
for port in 80 443; do
    if ! sudo iptables -C INPUT -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT 2>/dev/null; then
        # Inserted at the top so it precedes Oracle's REJECT rule wherever that sits.
        sudo iptables -I INPUT 1 -p tcp --dport "$port" -m conntrack --ctstate NEW -j ACCEPT
    fi
done
sudo netfilter-persistent save >/dev/null
sudo iptables -S INPUT | grep -E "dport (80|443) " || true

echo "== 5/6 project environment"
if [[ ! -d "$REPO_DIR/.git" ]]; then
    git clone "$REPO_URL" "$REPO_DIR"
fi
cd "$REPO_DIR/server"
# The lock is the source of truth; the `voice` extra is the bot (README, "The voice extra").
uv sync --frozen --all-extras --no-dev
uv run --no-sync python -c "import pipecat, fastembed, aiortc; print('pipecat', pipecat.__version__, 'ok')"

echo "== 6/6 Caddyfile + systemd units"
sed "s#<YOUR_HOSTNAME>#$HOSTNAME_ARG#g" "$HERE/Caddyfile" | sudo tee /etc/caddy/Caddyfile >/dev/null
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
sudo systemctl enable --now caddy
sudo systemctl reload caddy
for unit in ai-agent-bot ai-agent-app; do
    sed "s#/home/ubuntu#$HOME#g" "$HERE/$unit.service" | sudo tee "/etc/systemd/system/$unit.service" >/dev/null
done
sudo systemctl daemon-reload
sudo systemctl enable ai-agent-bot ai-agent-app

PUBLIC_IP="$(curl -s --max-time 5 https://ifconfig.me || true)"
RESOLVED="$(getent hosts "$HOSTNAME_ARG" | awk '{print $1}' | head -1 || true)"
echo
echo "Setup done."
echo "  hostname     $HOSTNAME_ARG  ->  resolves to ${RESOLVED:-nothing yet}"
echo "  this VM      public IP ${PUBLIC_IP:-unknown}"
if [[ -n "$RESOLVED" && -n "$PUBLIC_IP" && "$RESOLVED" != "$PUBLIC_IP" ]]; then
    echo "  WARNING: DNS does not point at this VM yet; Caddy cannot get a certificate until it does."
fi
if [[ ! -f "$REPO_DIR/server/.env" ]]; then
    echo
    echo "Next: copy server/.env from your PC (README.md step 9), edit TELEPHONY_PUBLIC_URL, then:"
    echo "  sudo systemctl start ai-agent-bot ai-agent-app"
fi
