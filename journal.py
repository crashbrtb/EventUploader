"""Reads tournament results out of the game's traffic.

What the game sends (confirmed on a real recording, 12/09/2026):

- Journal list (WebSocket): one entry per message, among them
  `[uuid, ended_at, n, "global_tournament_user_result", [{blob: [[kind, null, [type, variant], has_details]]}], ..., result_uid]`.
  `ended_at` is when the tournament closed (UTC seconds), `[type, variant]` is
  the tournament type and `result_uid` points at the detail message.
- Detail (WebSocket, when "Show details" is opened):
  `{result_uid: [{blob: [["global_tournament_user_result", [type, variant], n, [[1, 1, [[[player_id], points], ...], [], []]]]]}]}`.
  The ranking is already in order and includes players with 0 points.
- Player profiles (HTTP, route 402): rows `[[player_id], account, name, country, ..., power at [10], ..., clan tag at [13], ...]`.
  The ranking carries no names: they come from here, joined by player id.

The game never sends the tournament's name. The site remembers it per type.

Nothing here knows about the network or the window, so it can be tested with
hand-built packets and fed from a live capture or a saved recording alike.
"""
from __future__ import annotations

import base64
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

import tbcodec

USER_RESULT = "global_tournament_user_result"
PROFILES_ROUTE = 402
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
UNKNOWN_NAME_PREFIX = "id:"


@dataclass
class Profile:
    player_id: int
    name: str
    power: Optional[int] = None
    clan_tag: Optional[str] = None


@dataclass
class RankingRow:
    position: int
    player_id: int
    points: int
    name: Optional[str] = None
    power: Optional[int] = None

    @property
    def display_name(self) -> str:
        return self.name if self.name else f"{UNKNOWN_NAME_PREFIX}{self.player_id}"


@dataclass
class Tournament:
    result_uid: str
    tournament_key: Optional[str] = None
    ended_at: Optional[int] = None
    has_details: bool = True
    rows: List[RankingRow] = field(default_factory=list)
    seen_at: float = 0.0

    @property
    def has_ranking(self) -> bool:
        return bool(self.rows)

    @property
    def ended_at_iso(self) -> Optional[str]:
        if self.ended_at is None:
            return None
        return datetime.fromtimestamp(self.ended_at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    @property
    def ended_at_text(self) -> str:
        if self.ended_at is None:
            return ""
        return datetime.fromtimestamp(self.ended_at, timezone.utc).strftime("%d/%m/%Y %H:%M")

    @property
    def unnamed(self) -> int:
        return sum(1 for r in self.rows if not r.name)


def _iter_nodes(node: Any, depth: int = 0) -> Iterator[Any]:
    """Every list and dict in a tree, parents before children."""
    stack: List[Tuple[Any, int]] = [(node, depth)]
    while stack:
        current, level = stack.pop()
        if level > 60:
            continue
        if isinstance(current, (list, dict)):
            yield current
            children = current.values() if isinstance(current, dict) else current
            for child in children:
                if isinstance(child, (list, dict)):
                    stack.append((child, level + 1))


def _blob_messages(node: Any) -> List[Any]:
    """The messages inside a `{"blob": [...]}` wrapper, or [] when it is not one."""
    if isinstance(node, dict) and set(node.keys()) == {"blob"} and isinstance(node["blob"], list):
        return node["blob"]
    return []


def _type_key(value: Any) -> Optional[str]:
    if isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return ":".join(str(v) for v in value[:3])
    return None


def _first_blob(node: Any) -> Optional[list]:
    """The first message inside the first blob of a list, if any."""
    if not isinstance(node, list):
        return None
    for item in node:
        for message in _blob_messages(item):
            if isinstance(message, list):
                return message
    return None


def parse_journal_entry(entry: Any) -> Optional[Tournament]:
    """A Journal list entry for a user tournament result."""
    if not (isinstance(entry, list) and len(entry) >= 6):
        return None
    if entry[3] != USER_RESULT or not isinstance(entry[1], int) or not isinstance(entry[0], str):
        return None
    result_uid = entry[-1]
    if not (isinstance(result_uid, str) and UUID_RE.match(result_uid)):
        return None
    message = _first_blob(entry[4]) if len(entry) > 4 else None
    key = None
    has_details = True
    if isinstance(message, list) and message and message[0] == USER_RESULT:
        key = next((k for k in (_type_key(v) for v in message[1:3]) if k), None)
        if len(message) > 3 and isinstance(message[3], int):
            has_details = message[3] != 0
    return Tournament(result_uid=result_uid, tournament_key=key, ended_at=entry[1], has_details=has_details)


def parse_ranking_message(message: Any) -> Optional[Tuple[Optional[str], List[Tuple[int, int]]]]:
    """`[kind, [type, variant], n, [[1, 1, rows, ...]]]` -> (key, [(player_id, points)])."""
    if not (isinstance(message, list) and len(message) >= 4 and message[0] == USER_RESULT):
        return None
    key = _type_key(message[1])
    for node in _iter_nodes(message[3]):
        if not (isinstance(node, list) and node):
            continue
        pairs = []
        for row in node:
            if (
                isinstance(row, list) and len(row) == 2
                and isinstance(row[0], list) and row[0] and isinstance(row[0][0], int)
                and isinstance(row[1], int) and not isinstance(row[1], bool)
            ):
                pairs.append((row[0][0], row[1]))
            else:
                pairs = []
                break
        if pairs:
            return key, pairs
    return None


def parse_profile_row(row: Any) -> Optional[Profile]:
    if not (isinstance(row, list) and len(row) > 10):
        return None
    if not (isinstance(row[0], list) and row[0] and isinstance(row[0][0], int)):
        return None
    if not isinstance(row[2], str) or not row[2].strip():
        return None
    power = row[10] if isinstance(row[10], int) and not isinstance(row[10], bool) else None
    tag = row[13] if len(row) > 13 and isinstance(row[13], str) else None
    return Profile(player_id=row[0][0], name=row[2], power=power, clan_tag=tag)


class JournalCollector:
    """Accumulates what the traffic reveals and assembles whole tournaments."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: Dict[str, Tournament] = {}
        self.rankings: Dict[str, Tuple[Optional[str], List[Tuple[int, int]], float]] = {}
        self.profiles: Dict[int, Profile] = {}
        self.packets = 0

    # ----------------------------------------------------------------- input
    def feed_record(self, record: dict) -> bool:
        """Take one record from capture.CdpCapture. True when something was learned."""
        kind = record.get("kind")
        routes = record.get("routes") or []
        # Map chunks and the like are large and never hold what is needed here.
        if kind == "RESP" and PROFILES_ROUTE not in routes:
            return False
        try:
            tree = tbcodec.decode_payload(base64.b64decode(record.get("b64", "")))
        except Exception:
            return False
        return self.feed_tree(tree, record.get("ts", 0.0))

    def feed_tree(self, tree: Any, seen_at: float = 0.0) -> bool:
        if not isinstance(tree, dict):
            return False
        learned = False
        with self.lock:
            self.packets += 1
            if "frames" in tree:
                for frame in tree["frames"]:
                    if frame.get("route") == PROFILES_ROUTE:
                        learned |= self._take_profiles(frame.get("objects"))
            if "stream" in tree:
                learned |= self._take_stream(tree["stream"], seen_at)
        return learned

    def _take_profiles(self, objects: Any) -> bool:
        learned = False
        for node in _iter_nodes(objects):
            if not isinstance(node, list):
                continue
            for row in node:
                profile = parse_profile_row(row)
                if profile is not None:
                    self.profiles[profile.player_id] = profile
                    learned = True
        return learned

    def _take_stream(self, stream: Any, seen_at: float) -> bool:
        learned = False
        for node in _iter_nodes(stream):
            if isinstance(node, list):
                entry = parse_journal_entry(node)
                if entry is not None:
                    entry.seen_at = seen_at
                    self.entries[entry.result_uid] = entry
                    learned = True
            elif isinstance(node, dict):
                for key, value in node.items():
                    if not (isinstance(key, str) and UUID_RE.match(key)):
                        continue
                    message = _first_blob(value)
                    parsed = parse_ranking_message(message)
                    if parsed is not None:
                        self.rankings[key] = (parsed[0], parsed[1], seen_at)
                        learned = True
        return learned

    # ---------------------------------------------------------------- output
    def tournaments(self) -> List[Tournament]:
        """Every tournament with a ranking, plus Journal entries still to be opened."""
        with self.lock:
            out: Dict[str, Tournament] = {}
            for uid, entry in self.entries.items():
                if entry.has_details:
                    out[uid] = Tournament(uid, entry.tournament_key, entry.ended_at, True, [], entry.seen_at)
            for uid, (key, pairs, seen_at) in self.rankings.items():
                tournament = out.get(uid) or Tournament(uid, key, None, True, [], seen_at)
                tournament.tournament_key = tournament.tournament_key or key
                tournament.seen_at = max(tournament.seen_at, seen_at)
                tournament.rows = [
                    RankingRow(
                        position=index + 1,
                        player_id=player_id,
                        points=points,
                        name=self.profiles[player_id].name if player_id in self.profiles else None,
                        power=self.profiles[player_id].power if player_id in self.profiles else None,
                    )
                    for index, (player_id, points) in enumerate(pairs)
                ]
                out[uid] = tournament
        return sorted(out.values(), key=lambda t: (not t.has_ranking, -(t.ended_at or t.seen_at or 0)))

    def clear(self) -> None:
        with self.lock:
            self.entries.clear()
            self.rankings.clear()
            self.profiles.clear()
            self.packets = 0


def checks(tournament: Tournament) -> List[Tuple[bool, str]]:
    """What to look at before sending."""
    rows = tournament.rows
    out: List[Tuple[bool, str]] = []
    out.append((0 < len(rows) <= 100, f"{len(rows)} jogadores no ranking (esperado: 1 a 100)"))
    decreasing = all(a.points >= b.points for a, b in zip(rows, rows[1:]))
    out.append((decreasing, "pontos em ordem decrescente"))
    ids = [r.player_id for r in rows]
    out.append((len(set(ids)) == len(ids), "sem jogadores repetidos"))
    missing = tournament.unnamed
    out.append((missing == 0, "todos com nome" if missing == 0 else
                f"{missing} jogador(es) sem nome: feche e abra o 'Show details' de novo para o jogo enviar os perfis"))
    out.append((tournament.ended_at is not None, "data de término conhecida" if tournament.ended_at is not None else
                "data de término desconhecida: abra o Journal (a lista) com o app conectado, ou informe a data"))
    out.append((tournament.tournament_key is not None, f"tipo do torneio: {tournament.tournament_key or 'desconhecido'}"))
    return out


def build_payload(tournament: Tournament, name: str, ended_at_iso: Optional[str], client_version: str) -> dict:
    """The body of POST /api/v1/tournaments."""
    return {
        "result_uid": tournament.result_uid,
        "tournament_key": tournament.tournament_key or "0",
        "name": name.strip() or None,
        "ended_at": ended_at_iso,
        "capture_method": "packet",
        "client_version": client_version,
        "rows": [
            {
                "position": row.position,
                "name": row.display_name[:60],
                "points": row.points,
                "player_id": row.player_id,
                "power": row.power,
            }
            for row in tournament.rows
        ],
    }
