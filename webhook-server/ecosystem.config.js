module.exports = {
  apps: [{
    name: 'mailagent-webhook',
    script: 'app.py',
    interpreter: 'python3',
    interpreter_args: '-m uvicorn app:app --host 127.0.0.1 --port 8100',
    script: './',
    instances: 1,
    exec_mode: 'fork',
    watch: false,
    max_memory_restart: '500M',
    env: {
      REDIS_URL: 'redis://localhost:6379',
      REDIS_DB: '2',
      WEBHOOK_SECRET: '',
    },
    error_file: './logs/pm2-error.log',
    out_file: './logs/pm2-out.log',
    time: true,
    autorestart: true,
    max_restarts: 10,
    min_uptime: '10s',
    restart_delay: 4000
  }]
};
