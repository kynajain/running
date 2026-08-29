'use strict';

// Starts a public HTTPS tunnel to the configured listenerPort and prints the
// webhook URL to paste into the Terra dashboard. Development only — the URL
// changes every restart unless the tunnel has a reserved domain.

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const http = require('http');
const path = require('path');

const port = (() => {
  try {
    return JSON.parse(fs.readFileSync(path.join(__dirname, 'config.json'), 'utf8')).listenerPort;
  } catch {
    return 4300;
  }
})();

const has = (bin) => spawnSync('which', [bin]).status === 0;

function announce(publicUrl) {
  console.log('\n  Public URL:  %s', publicUrl);
  console.log('  Terra callback to paste into the dashboard:\n    %s/webhook/terra', publicUrl);
  console.log('\n  This URL changes when the tunnel restarts unless you configure a reserved');
  console.log('  domain. Update the Terra dashboard every time it changes.\n');
}

// ngrok exposes the tunnel it just opened on its local API.
function pollNgrok(attempt = 0) {
  http
    .get('http://127.0.0.1:4040/api/tunnels', (res) => {
      let body = '';
      res.on('data', (chunk) => (body += chunk));
      res.on('end', () => {
        const tunnel = (JSON.parse(body).tunnels || []).find((t) => t.proto === 'https');
        if (tunnel) announce(tunnel.public_url);
        else if (attempt < 20) setTimeout(() => pollNgrok(attempt + 1), 500);
      });
    })
    .on('error', () => {
      if (attempt < 20) setTimeout(() => pollNgrok(attempt + 1), 500);
    });
}

if (has('ngrok')) {
  console.log('Starting ngrok tunnel to 127.0.0.1:%d …', port);
  const child = spawn('ngrok', ['http', String(port), '--log', 'stdout'], { stdio: 'inherit' });
  pollNgrok();
  child.on('exit', (code) => process.exit(code || 0));
} else if (has('cloudflared')) {
  console.log('ngrok not found; starting a Cloudflare tunnel to 127.0.0.1:%d …', port);
  console.log('Look for the trycloudflare.com URL below, then append /webhook/terra.\n');
  const child = spawn('cloudflared', ['tunnel', '--url', `http://127.0.0.1:${port}`], {
    stdio: 'inherit',
  });
  child.on('exit', (code) => process.exit(code || 0));
} else {
  console.error('Neither ngrok nor cloudflared is installed.');
  console.error('  ngrok:       https://ngrok.com/download  (then `ngrok config add-authtoken …`)');
  console.error('  cloudflared: https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/downloads/');
  process.exit(1);
}
