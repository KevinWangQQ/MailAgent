#!/bin/bash
# 一键部署 webhook-server 到远程服务器
# Usage: ./scripts/deploy-webhook.sh

set -e

SERVER="106.52.146.114"
USER="root"
PASS_FILE="$HOME/.ssh/guangzhou_pass"
REMOTE_DIR="/home/lighthouse/MailAgent"

if [ ! -f "$PASS_FILE" ]; then
    echo "Error: Password file not found at $PASS_FILE"
    exit 1
fi

if ! command -v sshpass &> /dev/null; then
    echo "Error: sshpass not installed. Run: brew install sshpass"
    exit 1
fi

echo "Deploying webhook-server to $SERVER..."
sshpass -f "$PASS_FILE" ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password "$USER@$SERVER" << 'REMOTE_SCRIPT'
set -e
cd /home/lighthouse/MailAgent

echo "==> git pull"
git pull

cd webhook-server

# 确保 venv 存在，兼容不同 Python 版本
if [ ! -d "venv" ]; then
    echo "==> Creating venv..."
    python3 -m venv venv
fi

echo "==> Python version: $(./venv/bin/python3 --version)"

echo "==> Installing dependencies..."
./venv/bin/pip install -r requirements.txt -q

echo "==> Restarting PM2..."
pm2 restart mailagent-webhook
pm2 status mailagent-webhook

echo "==> Deploy complete!"
REMOTE_SCRIPT

echo "Done."
