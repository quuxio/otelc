import { appendFileSync, readFileSync } from 'node:fs';
import { ClientRequest, IncomingMessage, createServer, request } from 'node:http';
import { createServer as createTlsServer, request as tlsRequest } from 'node:https';
import { createInterface } from 'node:readline';
import { setImmediate as tick } from 'node:timers/promises';
const [role, argument, downstream, certificate, key] = process.argv.slice(2);
const input = createInterface({ input: process.stdin });
async function workloadRequest(target, message) {
    let reads = 0;
    const failure = new Error();
    Object.defineProperties(failure, Object.fromEntries(['name', 'message', 'stack'].map(key => [key,
        { configurable: true, get() { reads++; return 'application error'; } }])));
    const repeated = ['one', 'two'];
    Object.freeze(repeated);
    const headers = message.shape === 'raw'
        ? Object.freeze(['Host', target.host, 'X-Business', 'unchanged', 'X-Repeated', 'one', 'X-Repeated', 'two'])
        : Object.freeze({ 'X-Business': 'unchanged', 'X-Repeated': repeated });
    const original = JSON.stringify(headers);
    const options = { hostname: target.hostname, port: target.port,
        protocol: target.protocol, path: message.path, method: 'GET', agent: false, headers,
        ca: certificate ? readFileSync(certificate) : undefined };
    const send = target.protocol === 'https:' ? tlsRequest : request;
    return new Promise(resolve => {
        let ended = false;
        const outgoing = send(options, function (response) {
            const receiver = this === outgoing;
            let body = '';
            response.setEncoding('utf8');
            response.on('data', chunk => { body += chunk; });
            response.on('end', () => resolve({ status: response.statusCode, body,
                unchanged: original === JSON.stringify(headers), requestInstance: outgoing instanceof ClientRequest,
                responseInstance: response instanceof IncomingMessage, callbackReceiver: receiver, endIdentity: ended }));
        });
        outgoing.on('error', error => resolve({ unchanged: original === JSON.stringify(headers),
            requestInstance: outgoing instanceof ClientRequest, endIdentity: ended,
            errorIdentity: error === failure, errorReads: reads }));
        if (message.destroy)
            outgoing.destroy(failure);
        else
            ended = outgoing.end() === outgoing;
    });
}
async function workloadHandle(incoming, response) {
    appendFileSync(argument, JSON.stringify({ path: incoming.url, headers: incoming.headers,
        raw: incoming.rawHeaders }) + '\n');
    await Promise.resolve();
    if (incoming.url === '/relay') {
        const result = await workloadRequest(new URL(downstream), { path: '/sink' });
        response.end(result.body);
        return;
    }
    response.statusCode = incoming.url === '/error' ? 500 : 200;
    response.end(incoming.url === '/error' ? 'bad' : incoming.headers['x-business'] === 'unchanged' ? '8' : 'missing-header');
}
if (role === 'server') {
    const active = new Set();
    let pending = 0;
    const handle = (incoming, response) => {
        active.add(response);
        pending++;
        response.on('close', () => active.delete(response));
        void workloadHandle(incoming, response).finally(() => { pending--; });
    };
    const server = certificate && key ? createTlsServer({ cert: readFileSync(certificate), key: readFileSync(key) }, handle)
        : createServer(handle);
    server.listen(0, '127.0.0.1', () => {
        const address = server.address();
        if (address && typeof address !== 'string')
            console.log(JSON.stringify({ port: address.port }));
    });
    input.on('line', async (line) => {
        if (line === 'quit') {
            input.close();
            server.close();
            return;
        }
        if (line === 'barrier') {
            while (active.size || pending)
                await tick();
            console.log(JSON.stringify({ idle: true }));
        }
    });
}
else if (role === 'client') {
    const target = new URL(argument);
    input.on('line', async (line) => {
        if (line === 'quit') {
            input.close();
            return;
        }
        const result = await workloadRequest(target, JSON.parse(line));
        console.log(JSON.stringify(result));
    });
    console.log(JSON.stringify({ ready: true }));
}
else
    throw new Error('expected server or client role');
