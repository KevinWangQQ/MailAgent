module.exports = {
  apps: [{
    name: 'mailagent-webhook',
    script: 'start.py',
    interpreter: './venv/bin/python3.9',
    cwd: '/home/lighthouse/MailAgent/webhook-server',
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    max_memory_restart: '500M',
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    time: true,
    autorestart: true,
    max_restarts: 10,
    min_uptime: '10s',
    restart_delay: 4000
  }]
};
