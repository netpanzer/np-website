"""
Client for the netPanzer master server and game server query protocols.

This module is deliberately free of Django imports so the parsers can be unit
tested on their own.

Master server (TCP, default port 28900):
    -> \\list\\gamename\\netpanzer\\final\\
    <- \\ip\\<host>\\port\\<port>\\ip\\...\\final\\

Game server (UDP, the server's own game port):
    -> \\status\\final\\
    <- \\key\\value\\...\\final\\   (single datagram)

See the game sources for the authoritative field list:
  src/NetPanzer/Interfaces/InfoSocket.cpp (prepareStatusPacket)
  src/NetPanzer/Views/MainMenu/Multi/MasterServer/ServerQueryThread.cpp
"""

import logging
import select
import socket
import time

logger = logging.getLogger(__name__)

MASTER_LIST_QUERY = b"\\list\\gamename\\netpanzer\\final\\"
STATUS_QUERY = b"\\status\\final\\"

TERMINATOR = "\\final\\"

# A status reply is a single datagram. It grows with the player count, so give
# it plenty of room rather than assuming it stays under an MTU.
UDP_RECV_SIZE = 65535

# Everything here arrives from a machine we do not control: any game server can
# register with the master server and then answer with whatever it likes. Cap
# the strings to the width of the database columns that hold them (see
# website/models.py) so an oversized hostname or player name cannot become a
# DataError that rolls back a whole ingest. SQLite would quietly accept it
# today, which is exactly what makes this worth pinning down here.
MAX_FIELD_LENGTH = 255

# A 64KB datagram cannot hold many more than this, but bound it anyway so one
# server cannot drive an unbounded number of writes per collection.
MAX_PLAYERS = 512

# Fields we coerce to int when present.
_INT_FIELDS = (
    "protocol",
    "numplayers",
    "maxplayers",
    "units_per_player",
    "timelimit",
    "fraglimit",
    "objectivelimit",
    "time",
)

# Per-player fields, keyed by "<name>_<index>" in the raw reply.
_PLAYER_INT_FIELDS = ("kills", "deaths", "score", "points", "flag", "flagu")


def _tokenize(text):
    """Split a backslash-delimited payload into tokens, dropping empties."""
    return [token for token in text.split("\\") if token != ""]


def _clamp(value):
    """Bound an untrusted string to the width we are prepared to store."""
    return value[:MAX_FIELD_LENGTH]


def _to_int(value, default=0):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_master_list(text):
    """
    Parse a master server reply into a list of (host, port) tuples.

    Tolerates a reply that has been truncated mid-stream: any trailing
    incomplete \\ip\\..\\port\\.. pair is simply dropped.
    """
    tokens = _tokenize(text)
    servers = []
    seen = set()

    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == "final":
            break
        if token != "ip":
            # Unexpected token; the game client bails out here too.
            break
        if i + 3 >= len(tokens) or tokens[i + 2] != "port":
            break

        host = _clamp(tokens[i + 1])
        port = _to_int(tokens[i + 3])
        i += 4

        if not host or port <= 0 or port > 65535:
            continue
        key = (host, port)
        if key in seen:
            continue
        seen.add(key)
        servers.append(key)

    return servers


def parse_status(text):
    """
    Parse a game server \\status\\ reply into a dict.

    Returns the scalar fields plus a "players" list. Unknown keys are kept as
    strings so new server fields do not silently disappear.
    """
    tokens = _tokenize(text)

    fields = {}
    players_by_index = {}

    i = 0
    while i < len(tokens):
        key = tokens[i]
        if key == "final":
            break
        if i + 1 >= len(tokens):
            # Dangling key with no value (truncated reply).
            break
        value = tokens[i + 1]
        i += 2

        name, _, index = key.rpartition("_")
        if name and index.isdigit():
            slot = int(index)
            if slot >= MAX_PLAYERS:
                continue
            player = players_by_index.setdefault(slot, {})
            if name == "player":
                player["name"] = _clamp(value)
            elif name in _PLAYER_INT_FIELDS:
                player[name] = _to_int(value)
            else:
                player[name] = _clamp(value)
        else:
            fields[_clamp(key)] = _clamp(value)

    for field in _INT_FIELDS:
        if field in fields:
            fields[field] = _to_int(fields[field])

    for field in ("authentication", "password"):
        if field in fields:
            fields[field] = str(fields[field]).lower() == "y"

    players = []
    for index in sorted(players_by_index):
        player = players_by_index[index]
        if not player.get("name"):
            continue
        players.append(
            {
                "name": player["name"],
                "kills": _to_int(player.get("kills")),
                "deaths": _to_int(player.get("deaths")),
                "score": _to_int(player.get("score")),
                "points": _to_int(player.get("points")),
            }
        )

    fields["players"] = players
    return fields


def query_master(host, port=28900, timeout=2.5):
    """
    Ask the master server for the current server list.

    Returns a list of (host, port). Raises OSError on connection failure.
    """
    deadline = time.monotonic() + timeout
    chunks = []

    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.sendall(MASTER_LIST_QUERY)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                logger.warning("Master server %s:%s timed out", host, port)
                break
            sock.settimeout(remaining)
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                logger.warning("Master server %s:%s timed out", host, port)
                break
            if not chunk:
                break
            chunks.append(chunk)
            if TERMINATOR.encode() in b"".join(chunks):
                break

    return parse_master_list(b"".join(chunks).decode("utf-8", "replace"))


def query_servers(addresses, timeout=1.2, retries=1):
    """
    Query many game servers concurrently over a single UDP socket.

    Sends \\status\\final\\ to every address, then collects replies until the
    timeout, retrying the ones that did not answer. Total time is bounded by
    timeout * (retries + 1) rather than by the number of servers.

    Returns {(host, port): status_dict}, where status_dict includes "ping_ms".
    """
    addresses = list(dict.fromkeys(addresses))
    if not addresses:
        return {}

    results = {}
    pending = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setblocking(False)

        for attempt in range(retries + 1):
            outstanding = [addr for addr in addresses if addr not in results]
            if not outstanding:
                break

            for addr in outstanding:
                try:
                    sock.sendto(STATUS_QUERY, addr)
                    pending[addr] = time.monotonic()
                except OSError as error:
                    logger.warning("Failed to query %s:%s: %s", addr[0], addr[1], error)

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([sock], [], [], remaining)
                if not readable:
                    break
                try:
                    data, source = sock.recvfrom(UDP_RECV_SIZE)
                except OSError as error:
                    logger.warning("UDP receive failed: %s", error)
                    break

                if source not in pending or source in results:
                    # A late duplicate, or an unsolicited packet.
                    continue

                status = parse_status(data.decode("utf-8", "replace"))
                status["ping_ms"] = int((time.monotonic() - pending[source]) * 1000)
                results[source] = status

                if len(results) == len(addresses):
                    break

            if len(results) == len(addresses):
                break
    finally:
        sock.close()

    return results
