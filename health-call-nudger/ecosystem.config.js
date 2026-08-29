module.exports = {
  apps: [
    {
      name: 'health-call-nudger',
      script: 'health_call_nudger.js',
      cwd: __dirname,
      instances: 1,
      exec_mode: 'fork',
      autorestart: true,
      max_restarts: 10,
      restart_delay: 2000,
      watch: false,
      out_file: 'logs/health-call-nudger.out.log',
      error_file: 'logs/health-call-nudger.err.log',
      merge_logs: true,
      time: true,
      env: {
        NODE_ENV: 'production',
      },
    },
  ],
};
