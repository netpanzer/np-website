# Browser game deployment

The normal push-to-`main` deployment remains the deployment mechanism. No Node
service or separate listening port is needed: `run.sh` starts Uvicorn/ASGI on
the existing loopback port 8000. Django handles the website; the ASGI wrapper
streams versioned game assets and bridges `/play/game` WebSockets to native
game servers. Existing rankings, collection endpoints and the database are
unchanged.

## One-time reverse-proxy setup

Include `deploy/nginx-websocket.conf` inside the existing HTTPS `server` block,
then run `sudo nginx -t` before reloading nginx. Preserve the existing TLS,
HTTP redirect, `/static`, and normal proxy routes. No external gateway port
should be opened. The snippet overwrites forwarded client IPs, so browsers
cannot bypass per-IP limits with their own headers.

## Build and promote

1. In `netpanzer/netpanzer`, commit the game changes and push a tag named
   `web-<full-40-character-commit-SHA>` pointing to that commit. The Browser
   client workflow pins Emscripten 4.0.15, builds both the browser and native
   test server, runs Chrome/Firefox gameplay tests, and publishes a prerelease
   containing `netpanzer-web-<SHA>.tar.gz` and its `.sha256` file. It does not
   replace the desktop project's latest release.
2. Put the tested artifact's revision and archive checksum in this repo's
   `web-release.json`:

   ```json
   {"release": "<full commit SHA>", "sha256": "<archive SHA-256>"}
   ```

3. Run the Django tests, commit the pin with any website changes, and push
   `main`. The existing orchestrator deploys it. `run.sh` installs dependencies,
   runs migrations and collectstatic, then `sync_web_release` fetches the exact
   pinned GitHub release, checks its checksum and every extracted file, and
   atomically activates it before starting the app. An invalid download fails
   deployment rather than serving a partially upgraded game.

The release is never selected from a mutable `latest` URL. The installer rejects
unexpected files, traversal paths, symlinks, duplicate entries and size/hash
mismatches. Old releases remain available to already-loaded clients. Runtime
restarts will disconnect active players, who can reconnect through Join.

## Storage and rollback

`NP_WEB_ROOT` defaults to `browser-game` beside `NP_DB_PATH`. In production this
is `/home/winrid/np-website-data/browser-game`, outside the directory replaced
by rsync. Do not put it under the production checkout. Each release occupies
roughly 150–200 MB including gzip siblings; monitor disk use and retain the
previous release and any versions still in use when pruning manually.

To roll back durably, revert `web-release.json` to the previous revision and
checksum and push. A quick local activation is also available:

```sh
env/bin/python manage.py install_web_release --activate <previous-SHA>
```

The next deploy re-applies the committed pin. For local integration testing:

```sh
export NP_WEB_ROOT=/tmp/netpanzer-browser-releases
export DEBUG=True
env/bin/python manage.py install_web_release /path/to/build.tar.gz --sha256 <checksum>
env/bin/python manage.py runserver localhost:8001  # ordinary Django pages only
# Use ASGI for actual gameplay/assets:
env/bin/uvicorn netpanzer.asgi:application --host 127.0.0.1 --port 8001 --ws websockets-sansio
```

Open `http://localhost:8001/play/`. Production uses
`https://netpanzer.io/play/`. Assets have immutable revision URLs, gzip
precompression, correct Wasm MIME types and ETags. `/play/` and the live server
list are not cached.

## Server access and limits

The Join list uses the same master-server directory as the website. Players
may also type any public IPv4/hostname game endpoint; no operator allowlist is
needed. For an unlisted server, the password dialog accepts a blank password.
The gateway resolves DNS once, rejects private/special/mixed DNS answers, pins
the resulting numeric IP, and verifies the native NetPanzer UDP status reply
on that same game port before opening TCP. A server must expose its normal
public status service; private/LAN-only and status-disabled servers cannot be
reached through this public website gateway.

Only configured website origins can connect. Defaults: 64 concurrent connections,
8 per IP, 20 attempts per IP per minute, 1 MiB per binary message. The app uses
one ASGI worker so limits are process-wide; use shared counters before adding
workers or replicas. `NP_WEB_MAX_CONNECTIONS` and
`NP_WEB_MAX_CONNECTIONS_PER_IP` override connection limits. Configure
`NP_MASTER_SERVER` and `NP_MASTER_PORT` as with the existing site.

## Smoke checks

Check `/`, `/servers`, `/api/v1/servers`, `/play/`, compressed Wasm/data responses,
and an actual WebSocket game join in both Chrome and Firefox. Verify wrong
origins and private destinations are rejected. `manage.py test` covers archive
integrity/rollback, assets and gateway behavior. The game repo retains its
native-server Chrome/Firefox movement and reconnect tests.
