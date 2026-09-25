import asyncio
import gzip
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
from unittest import mock

from asgiref.testing import ApplicationCommunicator
from django.test import SimpleTestCase, override_settings

from website.browser_gateway import BrowserGateway, parse_destination, public_ipv4, resolve_public, verify_game_server
from website.web_releases import CLIENT_FILES, active_release, activate, install

REVISION = 'a' * 40


def release_archive(directory, revision=REVISION, extras=None):
    files = {'client/' + name: b'test file: ' + name.encode() for name in CLIENT_FILES}
    files['client/netpanzer.wasm.gz'] = gzip.compress(files['client/netpanzer.wasm'])
    manifest = {'schema': 1, 'release': revision, 'files': {
        name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()} for name, data in files.items()}}
    files['manifest.json'] = json.dumps(manifest).encode()
    files.update(extras or {})
    archive = Path(directory) / (revision + '.tar.gz')
    with tarfile.open(archive, 'w:gz') as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return archive, hashlib.sha256(archive.read_bytes()).hexdigest()


class ReleasesTest(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / 'browser-game'
        self.enterContext(override_settings(NP_WEB_ROOT=self.root))

    def test_install_is_verified_idempotent_and_activates(self):
        archive, checksum = release_archive(self.temp.name)
        self.assertEqual(install(archive, checksum, self.root), REVISION)
        self.assertEqual(active_release()['release'], REVISION)
        install(archive, checksum, self.root)
        self.assertEqual((self.root / 'current' / 'archive.sha256').read_text().strip(), checksum)

    def test_wrong_checksum_does_not_activate(self):
        archive, _ = release_archive(self.temp.name)
        with self.assertRaisesRegex(ValueError, 'SHA-256'):
            install(archive, '0' * 64, self.root)
        self.assertIsNone(active_release())

    def test_path_traversal_is_rejected(self):
        archive, checksum = release_archive(self.temp.name, extras={'../outside': b'bad'})
        with self.assertRaises(ValueError):
            install(archive, checksum, self.root)
        self.assertFalse((self.root / 'outside').exists())

    def test_modified_file_is_rejected(self):
        archive, checksum = release_archive(self.temp.name, extras={'client/launcher.js': b'wrong contents'})
        with self.assertRaises(ValueError):
            install(archive, checksum, self.root)
        self.assertIsNone(active_release())

    def test_rollback_keeps_old_asset_urls(self):
        archive, checksum = release_archive(self.temp.name)
        install(archive, checksum, self.root)
        archive, checksum = release_archive(self.temp.name, revision='b' * 40)
        install(archive, checksum, self.root)
        activate(self.root, REVISION)
        self.assertEqual(active_release()['release'], REVISION)
        self.assertTrue((self.root / 'releases' / ('b' * 40) / 'client/netpanzer.wasm').is_file())

    def test_play_page_unavailable_until_install(self):
        self.assertEqual(self.client.get('/play/').status_code, 503)
        archive, checksum = release_archive(self.temp.name)
        install(archive, checksum, self.root)
        response = self.client.get('/play/')
        self.assertContains(response, '/play/client/' + REVISION + '/index.html')
        self.assertContains(response, 'public=1')
        self.assertEqual(response['Cache-Control'], 'no-store')

    async def test_assets_are_compressed_cached_and_versioned(self):
        archive, checksum = release_archive(self.temp.name)
        install(archive, checksum, self.root)
        gateway = BrowserGateway(None)
        scope = {'type': 'http', 'method': 'GET', 'path': f'/play/client/{REVISION}/netpanzer.wasm',
                 'headers': [(b'accept-encoding', b'gzip')]}
        client = ApplicationCommunicator(gateway, scope)
        response = await client.receive_output()
        self.assertEqual(response['status'], 200)
        headers = dict(response['headers'])
        self.assertEqual(headers[b'content-type'], b'application/wasm')
        self.assertEqual(headers[b'content-encoding'], b'gzip')
        data = await client.receive_output()
        self.assertEqual(gzip.decompress(data['body']), b'test file: netpanzer.wasm')
        await client.wait()
        scope['headers'].append((b'if-none-match', headers[b'etag']))
        client = ApplicationCommunicator(gateway, scope)
        self.assertEqual((await client.receive_output())['status'], 304)
        await client.wait()
        scope['path'] = f'/play/client/{REVISION}/manifest.json'
        client = ApplicationCommunicator(gateway, scope)
        self.assertEqual((await client.receive_output())['status'], 404)
        await client.wait()


@override_settings(ALLOWED_HOSTS=['netpanzer.io'], DEBUG=False)
class GatewayTest(SimpleTestCase):
    def scope(self, origin=b'https://netpanzer.io', query=b'server=69.164.193.165:3031'):
        return {'type': 'websocket', 'path': '/play/game', 'headers': [(b'origin', origin)],
                'query_string': query, 'client': ('8.8.8.8', 1234), 'subprotocols': ['binary']}

    def test_destination_validation(self):
        self.assertEqual(parse_destination('game.example:3031'), ('game.example', 3031))
        for value in ('localhost:0', 'example.com:65536', 'http://example.com:80', 'example.com', '::1:3030'):
            with self.assertRaises(ValueError):
                parse_destination(value)

    def test_private_and_special_networks_are_blocked(self):
        for address in ('127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                        '0.0.0.0', '100.64.0.1', '224.0.0.1', '255.255.255.255', '::1'):
            self.assertFalse(public_ipv4(address), address)
        self.assertTrue(public_ipv4('69.164.193.165'))

    async def test_mixed_dns_answers_are_rejected(self):
        records = [(None, None, None, None, ('69.164.193.165', 3031)),
                   (None, None, None, None, ('127.0.0.1', 3031))]
        with mock.patch.object(asyncio.get_running_loop(), 'getaddrinfo', return_value=records):
            with self.assertRaises(ValueError):
                await resolve_public('game.example', 3031)

    async def test_origin_and_target_rejection_happen_before_tcp(self):
        for scope in (self.scope(origin=b'https://evil.example'), self.scope(origin=b'null'),
                      self.scope(origin=b'http://netpanzer.io'), self.scope(query=b'server=x:3030&host=internal'),
                      self.scope(query=b'server=x:3030&server=y:3030')):
            with mock.patch('website.browser_gateway.resolve_public') as resolve:
                client = ApplicationCommunicator(BrowserGateway(None), scope)
                await client.send_input({'type': 'websocket.connect'})
                self.assertEqual((await client.receive_output())['type'], 'websocket.close')
                await client.wait()
                resolve.assert_not_called()

    async def test_private_target_rejected_and_capacity_released(self):
        gateway = BrowserGateway(None)
        with mock.patch('website.browser_gateway.resolve_public', side_effect=ValueError('private')):
            client = ApplicationCommunicator(gateway, self.scope())
            await client.send_input({'type': 'websocket.connect'})
            self.assertEqual((await client.receive_output())['code'], 1008)
            await client.wait()
        self.assertFalse(gateway.connections)

    async def test_connection_limit_rejects_before_network_access(self):
        gateway = BrowserGateway(None)
        gateway.connections['8.8.8.8'] = 8
        with mock.patch('website.browser_gateway.resolve_public') as resolve:
            client = ApplicationCommunicator(gateway, self.scope())
            await client.send_input({'type': 'websocket.connect'})
            self.assertEqual((await client.receive_output())['code'], 1008)
            await client.wait()
            resolve.assert_not_called()

    async def test_status_probe_rejects_non_game_services(self):
        class Responder(asyncio.DatagramProtocol):
            def connection_made(self, transport):
                self.transport = transport

            def datagram_received(self, data, address):
                self.transport.sendto(b'HTTP/1.1 200 OK', address)

        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(Responder, local_addr=('127.0.0.1', 0))
        try:
            with self.assertRaisesRegex(ValueError, 'not a NetPanzer'):
                await verify_game_server('127.0.0.1', transport.get_extra_info('sockname')[1])
        finally:
            transport.close()

    async def test_binary_transport_and_disconnect_cleanup(self):
        async def echo(reader, writer):
            try:
                while chunk := await reader.read(65536):
                    writer.write(chunk)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(echo, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        gateway = BrowserGateway(None)
        try:
            with mock.patch('website.browser_gateway.resolve_public', return_value='127.0.0.1'), \
                    mock.patch('website.browser_gateway.verify_game_server') as verify:
                client = ApplicationCommunicator(gateway, self.scope(query=f'server=example.com:{port}'.encode()))
                await client.send_input({'type': 'websocket.connect'})
                self.assertEqual((await client.receive_output())['subprotocol'], 'binary')
                verify.assert_awaited_once_with('127.0.0.1', port)
                payload = bytes(range(256)) * 500
                await client.send_input({'type': 'websocket.receive', 'bytes': payload})
                result = b''
                while len(result) < len(payload):
                    result += (await client.receive_output())['bytes']
                self.assertEqual(result, payload)
                await client.send_input({'type': 'websocket.disconnect', 'code': 1000})
                await client.wait()
                self.assertFalse(gateway.connections)
        finally:
            server.close()
            await server.wait_closed()

    async def test_directory_maps_all_public_servers(self):
        snapshot = {'servers': [{'address': address, 'port': 3031, 'name': 'Game', 'online': True,
                                'num_players': 2, 'max_players': 8, 'protocol': 1128}
                               for address in ('69.164.193.165', '168.138.247.215', '127.0.0.1')]}
        with mock.patch('website.browser_gateway.services.get_live_servers', return_value=snapshot):
            client = ApplicationCommunicator(BrowserGateway(None), {'type': 'http', 'method': 'GET', 'path': '/play/servers'})
            self.assertEqual((await client.receive_output())['status'], 200)
            servers = json.loads((await client.receive_output())['body'])
            self.assertEqual(len(servers), 2)
            self.assertEqual(servers[0]['id'], '69.164.193.165:3031')
            await client.wait()
