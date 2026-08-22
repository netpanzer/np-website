#!/usr/bin/env python3
"""
Orchestrator background job entry point.

Pokes the site's stats collection endpoint so ranking and statistics keep
accumulating even when nobody is browsing. The site does the actual querying;
this only triggers it.

The orchestrator picks the interpreter from the file extension (.py -> python3),
so this needs no shell wrapper. Stdlib only - the job checks out the repo
without installing anything.

Environment:
    STATS_COLLECT_URL    defaults to https://netpanzer.io/api/v1/stats/collect
    STATS_COLLECT_TOKEN  shared secret, sent as X-Collect-Token
    STATS_COLLECT_TIMEOUT  seconds, defaults to 30
"""

import json
import os
import sys
import urllib.error
import urllib.request

DEFAULT_URL = 'https://netpanzer.io/api/v1/stats/collect'


def main():
    url = os.environ.get('STATS_COLLECT_URL', DEFAULT_URL)
    token = os.environ.get('STATS_COLLECT_TOKEN', '')
    timeout = float(os.environ.get('STATS_COLLECT_TIMEOUT', '30'))

    request = urllib.request.Request(url, data=b'', method='POST')
    if token:
        request.add_header('X-Collect-Token', token)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode('utf-8', 'replace')
            status = response.status
    except urllib.error.HTTPError as error:
        body = error.read().decode('utf-8', 'replace')
        print(f'{url} returned HTTP {error.code}: {body}', file=sys.stderr)
        return 1
    except (urllib.error.URLError, OSError, ValueError) as error:
        print(f'Failed to reach {url}: {error}', file=sys.stderr)
        return 1

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        print(f'{url} returned HTTP {status} with non-JSON body: {body[:500]}', file=sys.stderr)
        return 1

    if not payload.get('ok'):
        print(f'Collection reported failure: {body}', file=sys.stderr)
        return 1

    if payload.get('throttled'):
        print(f'Collection throttled, retry in {payload.get("retry_in")}s')
    else:
        print(
            f'Collected {payload.get("players", 0)} player row(s) from '
            f'{payload.get("servers", 0)} server(s) into {payload.get("month")}'
        )
    return 0


if __name__ == '__main__':
    sys.exit(main())
