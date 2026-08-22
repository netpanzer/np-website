"""
Two separate concerns, deliberately kept apart:

  * get_live_servers() backs the server browser. It is queried on request with
    a short in-memory cache, touches no database, and serves stale data rather
    than failing.
  * collect() backs the ranking and statistics pages. It runs from the cron
    endpoint / management command and accumulates player deltas into the DB.
"""

import logging
import threading
import time

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from website import gamequery
from website.models import (
    GameServer,
    PlayerAllTimeStat,
    PlayerMonthStat,
    PlayerRoundState,
    ServerActivitySample,
    ServerDailyStat,
)

logger = logging.getLogger(__name__)

# Keep at most one ingest per this many seconds, no matter who calls it.
INGEST_MIN_INTERVAL_SECONDS = 20

# How long raw samples are kept is settings.NP_RAW_SAMPLE_RETENTION_DAYS.
# Deliberately not mirrored here: only the raw detail rows expire, and the
# permanent history in ServerDailyStat is never deleted, so this is a number
# worth having exactly one definition of.

_cache_lock = threading.Lock()

# Replaced wholesale, never mutated in place, so readers can take a consistent
# (fetched_at, payload) snapshot with one atomic read and no lock at all.
_cache = (0.0, None)

_ingest_lock = threading.Lock()
_last_ingest_at = 0.0


def _fetch_servers():
    """Query the master server and every game server it lists."""
    host = settings.NP_MASTER_SERVER
    port = settings.NP_MASTER_PORT

    addresses = gamequery.query_master(host, port)
    statuses = gamequery.query_servers(addresses)

    servers = []
    for address, server_port in addresses:
        status = statuses.get((address, server_port))
        if status is None:
            servers.append({
                'address': address,
                'port': server_port,
                'online': False,
                'name': '',
                'players': [],
                'num_players': 0,
                'max_players': 0,
            })
            continue

        servers.append({
            'address': address,
            'port': server_port,
            'online': True,
            'name': status.get('hostname', ''),
            'map': status.get('mapname', ''),
            'map_style': status.get('mapstyle', ''),
            'game_style': status.get('gamestyle', ''),
            'version': status.get('gameversion', ''),
            'protocol': status.get('protocol', 0),
            'num_players': status.get('numplayers', 0),
            'max_players': status.get('maxplayers', 0),
            'password': status.get('password', False),
            'authentication': status.get('authentication', False),
            'units_per_player': status.get('units_per_player', 0),
            'time_limit': status.get('timelimit', 0),
            'frag_limit': status.get('fraglimit', 0),
            'objective_limit': status.get('objectivelimit', 0),
            'ping_ms': status.get('ping_ms', 0),
            'players': status.get('players', []),
        })

    # Populated servers first, then by name, so the interesting ones are on top.
    servers.sort(key=lambda s: (-s['num_players'], s['name'].lower()))
    return servers


def reset_cache():
    """Drop the cached server list. For tests; process-local state needs a seam."""
    global _cache
    _cache = (0.0, None)


def reset_throttle():
    """Forget when we last collected. For tests."""
    global _last_ingest_at
    _last_ingest_at = 0.0


def _snapshot(payload, age, stale=False):
    result = dict(payload)
    result['age_seconds'] = int(age)
    if stale:
        result['stale'] = True
    return result


def get_live_servers(force=False):
    """
    Return the current server list, cached in memory for a few seconds.

    Never raises: on a failed refresh the previous payload is returned with
    stale=True, and only an empty cache produces an error payload. This page
    spent months blank because it depended on something that could fail, so
    degrading is always preferred over erroring.

    A refresh takes seconds of network IO. Rather than queue behind one that is
    already in flight - which would tie up a worker to end up returning much
    the same thing - a reader that finds the lock held serves the previous
    payload as stale. Only a cold cache, or an explicit force from collect(),
    waits.
    """
    global _cache

    ttl = settings.NP_LIVE_CACHE_SECONDS
    fetched_at, cached = _cache
    age = time.monotonic() - fetched_at

    if not force and cached is not None and age < ttl:
        return _snapshot(cached, age)

    if force:
        _cache_lock.acquire()
    elif not _cache_lock.acquire(blocking=False):
        if cached is not None:
            return _snapshot(cached, age, stale=True)
        # Nothing to serve yet, so waiting for the in-flight refresh beats
        # returning an empty list.
        _cache_lock.acquire()

    try:
        # Another caller may have refreshed while we waited.
        fetched_at, cached = _cache
        age = time.monotonic() - fetched_at
        if not force and cached is not None and age < ttl:
            return _snapshot(cached, age)

        try:
            servers = _fetch_servers()
        except OSError as error:
            logger.warning('Failed to refresh server list: %s', error)
            if cached is not None:
                return _snapshot(cached, age, stale=True)
            return {
                'servers': [],
                'count': 0,
                'total_players': 0,
                'fetched_at': None,
                'age_seconds': 0,
                'stale': True,
                'error': str(error),
            }

        payload = {
            'servers': servers,
            'count': len(servers),
            'total_players': sum(s['num_players'] for s in servers),
            'fetched_at': timezone.now().isoformat(),
            'age_seconds': 0,
            'stale': False,
        }
        _cache = (time.monotonic(), payload)
        return dict(payload)
    finally:
        _cache_lock.release()


def is_round_reset(player, state):
    """
    Whether the server's counters restarted since we last looked.

    Kills and deaths only ever climb within a round, so either of them dropping
    means the round restarted or the player rejoined. Points and score are NOT
    usable for this - netPanzer lets them fall, and players can even finish a
    month on a negative score, so treating "points went down" as a reset would
    credit the same points over and over.
    """
    return player['kills'] < state.last_kills or player['deaths'] < state.last_deaths


def _deltas(player, state):
    """Turn absolute round counters into increments to add to the month total."""
    if is_round_reset(player, state):
        # Fresh round: everything on the board was earned since we last looked.
        return (player['kills'], player['deaths'], player['score'], player['points'])
    return (
        player['kills'] - state.last_kills,
        player['deaths'] - state.last_deaths,
        player['score'] - state.last_score,
        player['points'] - state.last_points,
    )


def current_month():
    return timezone.now().strftime('%Y-%m')


def _fit(model, field, value):
    """
    Trim a value to its column width before writing it.

    gamequery already clamps what it parses, but ingest() is the thing that
    touches the database and it runs inside one transaction: on any backend
    that enforces varchar widths (Postgres, MySQL - not SQLite) a single
    oversized name would raise DataError and roll back the entire snapshot,
    stalling collection for as long as that server stays registered. Reading
    the width off the field keeps this honest if a column is ever resized.
    """
    max_length = model._meta.get_field(field).max_length
    return value[:max_length] if max_length else value


@transaction.atomic
def ingest(servers, month=None):
    """
    Fold a server snapshot into the persistent stats.

    Returns a summary dict. Safe to call more often than needed: repeated
    snapshots with unchanged counters produce zero deltas.
    """
    now = timezone.now()
    month = month or now.strftime('%Y-%m')

    servers_seen = 0
    players_seen = 0
    kills_added = 0

    for entry in servers:
        if not entry.get('online'):
            continue
        servers_seen += 1

        address = _fit(GameServer, 'address', entry['address'])
        server_name = _fit(GameServer, 'name', entry.get('name', ''))

        server, _ = GameServer.objects.get_or_create(
            address=address,
            port=entry['port'],
            defaults={'name': server_name, 'first_seen': now},
        )
        updates = ['last_seen']
        server.last_seen = now
        if server_name and server.name != server_name:
            server.name = server_name
            updates.append('name')
        server.save(update_fields=updates)

        num_players = entry.get('num_players', 0)

        ServerActivitySample.objects.create(
            server=server,
            sampled_at=now,
            num_players=num_players,
        )

        # The permanent record. Rolled up here so it can never fall behind the
        # samples it summarises, and so it survives their pruning.
        daily, _ = ServerDailyStat.objects.get_or_create(
            server=server,
            day=now.date(),
        )
        daily.record(num_players)
        daily.save()

        for player in entry.get('players', []):
            name = (player.get('name') or '').strip()
            if not name:
                continue
            # Every table keys on this name, so they must be trimmed alike.
            name = _fit(PlayerMonthStat, 'player_name', name)
            name = _fit(PlayerAllTimeStat, 'player_name', name)
            name = _fit(PlayerRoundState, 'player_name', name)
            players_seen += 1

            state, created = PlayerRoundState.objects.get_or_create(
                server=server,
                player_name=name,
                defaults={
                    'last_kills': player['kills'],
                    'last_deaths': player['deaths'],
                    'last_score': player['score'],
                    'last_points': player['points'],
                    'updated_at': now,
                },
            )

            if created:
                # First sighting: count what they already have, so a player who
                # was mid-round when we started still gets credited.
                deltas = (
                    player['kills'],
                    player['deaths'],
                    player['score'],
                    player['points'],
                )
            else:
                deltas = _deltas(player, state)
                state.last_kills = player['kills']
                state.last_deaths = player['deaths']
                state.last_score = player['score']
                state.last_points = player['points']
                state.updated_at = now
                state.save()

            # The same deltas land in the month bucket and in the career total,
            # so the all-time board never needs to sum the monthly archive.
            month_stat, _ = PlayerMonthStat.objects.get_or_create(
                month=month,
                player_name=name,
                defaults={'first_seen': now, 'last_seen': now},
            )
            all_time, _ = PlayerAllTimeStat.objects.get_or_create(
                player_name=name,
                defaults={'first_seen': now, 'last_seen': now},
            )

            for stat in (month_stat, all_time):
                stat.kills += deltas[0]
                stat.deaths += deltas[1]
                stat.score += deltas[2]
                stat.points += deltas[3]
                stat.last_seen = now
                # save() refreshes the stored ranking metrics, so the read path
                # can sort in the database instead of in Python.
                stat.save()

            kills_added += deltas[0]

    # Only the raw detail rows expire. ServerDailyStat is the history and is
    # deliberately never touched here.
    cutoff = now - timezone.timedelta(days=settings.NP_RAW_SAMPLE_RETENTION_DAYS)
    pruned, _ = ServerActivitySample.objects.filter(sampled_at__lt=cutoff).delete()

    return {
        'month': month,
        'servers': servers_seen,
        'players': players_seen,
        'kills_added': kills_added,
        'pruned_samples': pruned,
    }


def collect(force=False):
    """
    Refresh the live list and fold it into the stats.

    Throttled process-wide so that a caller cannot make us hammer the master
    server. Returns (ran, summary).

    The lock is taken without blocking: a collection involves several seconds
    of network IO, and a second caller that waited for it would occupy a worker
    for that whole time only to be told it was throttled anyway. Answering
    immediately is the point of throttling.
    """
    global _last_ingest_at

    if not _ingest_lock.acquire(blocking=False):
        return False, {'throttled': True, 'reason': 'a collection is already running'}

    try:
        elapsed = time.monotonic() - _last_ingest_at
        if not force and _last_ingest_at and elapsed < INGEST_MIN_INTERVAL_SECONDS:
            return False, {
                'throttled': True,
                'retry_in': int(INGEST_MIN_INTERVAL_SECONDS - elapsed),
            }

        payload = get_live_servers(force=True)
        if payload.get('error'):
            raise OSError(payload['error'])

        summary = ingest(payload['servers'])
        _last_ingest_at = time.monotonic()
        return True, summary
    finally:
        _ingest_lock.release()
