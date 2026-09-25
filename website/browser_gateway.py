"""ASGI browser transport: public NetPanzer servers only, never arbitrary TCP."""
import asyncio
from collections import Counter, OrderedDict
import ipaddress
import json
from pathlib import Path
import re
import socket
import time
from urllib.parse import parse_qs, urlsplit

from django.conf import settings

from website import services
from website.web_releases import CLIENT_FILES

MAX_MESSAGE = 1024 * 1024
ASSET_PATH = re.compile(r'^/play/client/([0-9a-f]{40})/([^/]+)$')
MIME = {'.html': 'text/html; charset=utf-8', '.js': 'text/javascript',
        '.wasm': 'application/wasm', '.data': 'application/octet-stream', '.txt': 'text/plain'}


def public_ipv4(value):
    try:
        address = ipaddress.ip_address(value)
        return address.version == 4 and address.is_global and not address.is_multicast and not address.is_reserved
    except ValueError:
        return False


def parse_destination(value):
    if not re.fullmatch(r'[a-zA-Z0-9.-]{1,253}:[0-9]{1,5}', value):
        raise ValueError('Invalid server address')
    host, port = value.rsplit(':', 1)
    port = int(port)
    if not 0 < port < 65536:
        raise ValueError('Invalid game port')
    return host, port


async def resolve_public(host, port):
    records = await asyncio.get_running_loop().getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
    addresses = {record[4][0] for record in records}
    # Reject mixed public/private DNS answers too; then pin one numeric address
    # for both probes and the TCP connection to prevent DNS rebinding.
    if not addresses or not all(public_ipv4(address) for address in addresses):
        raise ValueError('Only public Internet game servers are supported')
    return sorted(addresses)[0]


async def verify_game_server(address, port):
    loop = asyncio.get_running_loop()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.setblocking(False)
        await loop.sock_connect(sock, (address, port))
        await loop.sock_sendall(sock, b'\\status\\final\\')
        reply = await loop.sock_recv(sock, 65535)
    tokens = reply.lstrip(b'\\').split(b'\\')
    fields = dict(zip(tokens[::2], tokens[1::2]))
    if fields.get(b'gamename') != b'netpanzer' or not fields.get(b'protocol', b'').isdigit():
        raise ValueError('Destination is not a NetPanzer server')


async def respond(send, status, body=b'', headers=()):
    await send({'type': 'http.response.start', 'status': status, 'headers': list(headers)})
    await send({'type': 'http.response.body', 'body': body})


class BrowserGateway:
    def __init__(self, django):
        self.django = django
        self.connections = Counter()
        self.attempts = OrderedDict()

    async def __call__(self, scope, receive, send):
        if scope['type'] == 'lifespan':
            while True:
                event = await receive()
                if event['type'] == 'lifespan.startup':
                    await send({'type': 'lifespan.startup.complete'})
                else:
                    await send({'type': 'lifespan.shutdown.complete'})
                    return
        elif scope['type'] == 'websocket':
            await self.websocket(scope, receive, send)
        elif scope['type'] == 'http' and scope['path'].startswith('/play/client/'):
            await self.asset(scope, receive, send)
        elif scope['type'] == 'http' and scope['path'] == '/play/servers':
            await self.servers(scope, send)
        else:
            await self.django(scope, receive, send)

    async def servers(self, scope, send):
        if scope['method'] != 'GET':
            return await respond(send, 405)
        snapshot = await asyncio.to_thread(services.get_live_servers)
        result = []
        for server in snapshot['servers']:
            # The directory uses IP addresses. Never present internal endpoints.
            if not public_ipv4(server['address']):
                continue
            result.append({'id': f"{server['address']}:{server['port']}",
                           'host': server['address'], 'port': server['port'],
                           'name': server['name'] or server['address'], 'map': server.get('map', '?'),
                           'players': server['num_players'], 'maxplayers': server['max_players'],
                           'protocol': server.get('protocol', -1), 'password': server.get('password', False),
                           'auth': server.get('authentication', False), 'ping': server.get('ping_ms', -1),
                           'running': server['online']})
        await respond(send, 503 if snapshot.get('error') else 200, json.dumps(result).encode(),
                      [(b'content-type', b'application/json'), (b'cache-control', b'no-store')])

    async def asset(self, scope, receive, send):
        match = ASSET_PATH.fullmatch(scope['path'])
        if not match or match[2] not in CLIENT_FILES:
            return await respond(send, 404)
        if scope['method'] not in ('GET', 'HEAD'):
            return await respond(send, 405)
        revision, filename = match.groups()
        path = Path(settings.NP_WEB_ROOT) / 'releases' / revision / 'client' / filename
        request_headers = dict(scope.get('headers', []))
        encodings = request_headers.get(b'accept-encoding', b'').decode('ascii', 'ignore').split(',')
        compressed = any(part.strip().split(';')[0] == 'gzip' and not re.search(r';\s*q=0(?:\.0*)?\s*$', part)
                         for part in encodings) and path.with_name(filename + '.gz').is_file()
        if compressed:
            path = path.with_name(filename + '.gz')
        try:
            file = await asyncio.to_thread(path.open, 'rb')
        except OSError:
            return await respond(send, 404)
        try:
            size = path.stat().st_size
            etag = f'"{revision}-{filename}{"-gzip" if compressed else ""}"'.encode()
            headers = [(b'content-type', MIME.get(Path(filename).suffix, 'application/octet-stream').encode()),
                       (b'cache-control', b'public, max-age=31536000, immutable'), (b'etag', etag),
                       (b'vary', b'Accept-Encoding'), (b'x-content-type-options', b'nosniff'),
                       (b'x-frame-options', b'SAMEORIGIN')]
            if request_headers.get(b'if-none-match') == etag:
                return await respond(send, 304, headers=headers)
            headers.append((b'content-length', str(size).encode()))
            if compressed:
                headers.append((b'content-encoding', b'gzip'))
            await send({'type': 'http.response.start', 'status': 200, 'headers': headers})
            if scope['method'] == 'GET':
                while chunk := await asyncio.to_thread(file.read, 512 * 1024):
                    await send({'type': 'http.response.body', 'body': chunk, 'more_body': True})
            await send({'type': 'http.response.body', 'body': b''})
        finally:
            file.close()

    def origin_allowed(self, origin):
        try:
            url = urlsplit(origin)
            if origin != f'{url.scheme}://{url.netloc}' or url.username or url.password:
                return False
            return url.hostname in settings.ALLOWED_HOSTS and (url.scheme == 'https' or
                   (settings.DEBUG and url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1')))
        except ValueError:
            return False

    async def websocket(self, scope, receive, send):
        if (await receive())['type'] != 'websocket.connect':
            return
        headers = dict(scope.get('headers', []))
        client = (scope.get('client') or ('unknown', 0))[0]
        now = time.monotonic()
        recent = [t for t in self.attempts.pop(client, []) if now - t < 60]
        self.attempts[client] = (recent + [now])[-20:]
        if len(self.attempts) > 4096:
            self.attempts.popitem(last=False)
        if (scope['path'] != '/play/game' or not self.origin_allowed(headers.get(b'origin', b'').decode('ascii', 'ignore')) or
                len(recent) >= 20 or sum(self.connections.values()) >= settings.NP_WEB_MAX_CONNECTIONS or
                self.connections[client] >= settings.NP_WEB_MAX_CONNECTIONS_PER_IP):
            return await send({'type': 'websocket.close', 'code': 1008})
        self.connections[client] += 1
        writer = None
        accepted = False
        disconnected = False
        tasks = []
        try:
            query = parse_qs(scope.get('query_string', b'').decode('ascii'), strict_parsing=True)
            if set(query) != {'server'} or len(query['server']) != 1:
                raise ValueError('Invalid server selection')
            host, port = parse_destination(query['server'][0])
            async with asyncio.timeout(6):
                address = await resolve_public(host, port)
                await verify_game_server(address, port)
                reader, writer = await asyncio.open_connection(address, port, family=socket.AF_INET)
            await send({'type': 'websocket.accept', 'subprotocol': 'binary' if 'binary' in scope.get('subprotocols', []) else None})
            accepted = True

            async def browser_to_game():
                nonlocal disconnected
                while True:
                    message = await receive()
                    if message['type'] == 'websocket.disconnect':
                        disconnected = True
                        return
                    data = message.get('bytes')
                    if data is None or len(data) > MAX_MESSAGE:
                        raise ValueError('Binary game messages only')
                    writer.write(data)
                    await writer.drain()

            async def game_to_browser():
                while data := await reader.read(65536):
                    await send({'type': 'websocket.send', 'bytes': data})

            tasks = [asyncio.create_task(browser_to_game()), asyncio.create_task(game_to_browser())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (OSError, ValueError, TimeoutError, UnicodeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if writer:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=2)
                except (OSError, TimeoutError):
                    writer.transport.abort()
            self.connections[client] -= 1
            if not self.connections[client]:
                del self.connections[client]
            if not disconnected:
                await send({'type': 'websocket.close', 'code': 1000 if accepted else 1008})
