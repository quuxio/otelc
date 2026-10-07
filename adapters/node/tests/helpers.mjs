import path from 'node:path';
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const root = path.resolve(fileURLToPath(new URL('../../../', import.meta.url)));
export const plan = endpoint => ({ language: 'javascript', execution_available: true, metrics_endpoint: endpoint,
  annotations: { read_existing: true, inject_generated: false },
  source_matchers: { include: ['(?-u).*'], exclude: [] }, function_matchers: { include: ['(?-u).*'], exclude: ['(?-u).*excluded'] },
  metrics: { enabled: true, histogram_boundaries_seconds: [0.001, 0.01, 1] },
  resource: { service_name: 'node-test', service_version: '1', attributes: {} },
  export: { interval_ms: 100, timeout_ms: 300 },
  runtime: { max_functions: 64, max_active_calls: 64, shutdown_timeout_ms: 500, control_socket: null } });

export async function receiver(handler) {
  const requests = [];
  const server = http.createServer((request, response) => {
    const chunks = [];
    request.on('data', chunk => chunks.push(chunk));
    request.on('end', () => { requests.push({ body: Buffer.concat(chunks), headers: request.headers, url: request.url }); if (handler) handler(response); else response.end(); });
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  return { requests, endpoint: `http://127.0.0.1:${server.address().port}/v1/metrics`, close: () => new Promise(resolve => server.close(resolve)) };
}
export async function control(filename, command) {
  return new Promise((resolve, reject) => {
    const socket = net.connect(filename); let result = '';
    socket.on('connect', () => socket.write(command));
    socket.on('error', reject); socket.on('data', value => { result += value; });
    socket.on('end', () => resolve(JSON.parse(result)));
  });
}
export async function child(command, args, environment = {}) {
  return new Promise((resolve, reject) => {
    const process = spawn(command, args, { cwd: root, env: { ...globalThis.process.env, ...environment } });
    let stdout = '', stderr = '';
    process.stdout.on('data', data => { stdout += data; }); process.stderr.on('data', data => { stderr += data; });
    process.on('error', reject); process.on('close', status => resolve({ status, stdout, stderr }));
  });
}

