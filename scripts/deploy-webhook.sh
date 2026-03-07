#!/bin/bash
# 一键部署 webhook-server 到远程服务器
# Usage: ./scripts/deploy-webhook.sh

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
sshpass -f "$PASS_FILE" ssh -o StrictHostKeyChecking=no -o PreferredAuthentications=password "$USER@$SERVER" \
    "cd $REMOTE_DIR && git pull && cd webhook-server && source venv/bin/activate && pip install -r requirements.txt -q && pm2 restart mailagent-webhook && pm2 status mailagent-webhook"

echo "Done."
