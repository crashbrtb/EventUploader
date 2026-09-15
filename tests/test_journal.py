"""Journal reader and API client.

Packets are built in the game's own shape, as seen on the recording of
12/09/2026 (see journal.py). Run:
    .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import base64
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import api  # noqa: E402
import journal  # noqa: E402
import tbcodec  # noqa: E402
from settings import Settings  # noqa: E402

RESULT_UID = "eede1d1c-3f13-481a-819d-51fdcebb1e54"
ENDED_AT = 1789232426  # 2026-09-12 17:00:26 UTC
PLAYERS = [(300647954111, "Naughtius Maximus", 1703103642), (438086712193, "Lion", 873578580),
           (4307852203038, "Lion", 503945198), (622770520040, "BENAR", 0)]


def ws_record(stream_objects, idx=1):
    data = b"".join(tbcodec.pack(o) for o in stream_objects)
    return {"idx": idx, "kind": "WS_IN", "ts": 100.0 + idx, "url": "wss://x", "routes": [],
            "b64": base64.b64encode(data).decode("ascii")}


def http_record(frame_bytes, idx=1):
    return {"idx": idx, "kind": "RESP", "ts": 100.0 + idx, "url": "https://game-us37.totalbattle.com/rubens-realm237",
            "routes": [f.route for f in tbcodec.iter_frames(frame_bytes) if f.route is not None],
            "b64": base64.b64encode(frame_bytes).decode("ascii")}


def journal_list(has_details=1, key=(1024, 1)):
    summary = tbcodec.pack(["global_tournament_user_result", None, list(key), has_details])
    entry = ["3cd16678-5f7b-44f8-847e-0bee495480d9", ENDED_AT, 1876146181246441, "global_tournament_user_result",
             [summary], True, False, False, RESULT_UID]
    noise = ["4285e683-78b8-4f1d-a6b9-8ab18d5f9af3", ENDED_AT - 50, 1, "treasury_buy", [b"\x94\x01\x02\x03\x04"], True, False, False, None]
    return ws_record([[{"733765024": b"\x93\xcd\x5e\x02\x00\xa0"}], [[noise, entry]]], idx=1)


def journal_detail(players=PLAYERS):
    ranking = [[[pid], points] for pid, _, points in players]
    inner = tbcodec.pack(["global_tournament_user_result", [1024, 1], 1, [[1, 1, ranking, [], []]]])
    return ws_record([[{"733765024": b"\x93\x06\x00\xa0"}], [{RESULT_UID: [inner, True]}]], idx=2)


def profiles(players=PLAYERS):
    rows = [[[pid], f"tb:{pid}", name, "BR", 3, 1, 2, 444, 45, 25, 1_000_000 + i, [4410931413044], 528144, "KOK"]
            for i, (pid, name, _) in enumerate(players)]
    return http_record(tbcodec.build_frame(402, 7, [rows]), idx=3)


class JournalTest(unittest.TestCase):
    def test_assembles_a_tournament_from_list_detail_and_profiles(self):
        collector = journal.JournalCollector()
        self.assertTrue(collector.feed_record(journal_list()))
        self.assertTrue(collector.feed_record(journal_detail()))
        self.assertTrue(collector.feed_record(profiles()))

        [tournament] = collector.tournaments()
        self.assertEqual(RESULT_UID, tournament.result_uid)
        self.assertEqual("1024:1", tournament.tournament_key)
        self.assertEqual("2026-09-12T17:00:26Z", tournament.ended_at_iso)
        self.assertEqual(["Naughtius Maximus", "Lion", "Lion", "BENAR"], [r.name for r in tournament.rows])
        self.assertEqual([1, 2, 3, 4], [r.position for r in tournament.rows])
        self.assertEqual(0, tournament.rows[3].points)
        self.assertEqual(0, tournament.unnamed)
        self.assertTrue(all(ok for ok, _ in journal.checks(tournament)))

        payload = journal.build_payload(tournament, " Rise of the Ancients ", tournament.ended_at_iso, "1.0.0")
        self.assertEqual("Rise of the Ancients", payload["name"])
        self.assertEqual("1024:1", payload["tournament_key"])
        self.assertEqual({"position": 3, "name": "Lion", "points": 503945198, "player_id": 4307852203038, "power": 1000002},
                         payload["rows"][2])

    def test_order_of_arrival_does_not_matter(self):
        collector = journal.JournalCollector()
        for record in (profiles(), journal_detail(), journal_list()):
            collector.feed_record(record)
        [tournament] = collector.tournaments()
        self.assertEqual("2026-09-12T17:00:26Z", tournament.ended_at_iso)
        self.assertEqual(4, len(tournament.rows))

    def test_detail_without_list_or_profiles_is_still_usable_but_flagged(self):
        collector = journal.JournalCollector()
        collector.feed_record(journal_detail())

        [tournament] = collector.tournaments()
        self.assertEqual("1024:1", tournament.tournament_key, "the detail carries the type too")
        self.assertIsNone(tournament.ended_at)
        self.assertEqual(4, tournament.unnamed)
        self.assertEqual("id:622770520040", tournament.rows[3].display_name)
        failing = [text for ok, text in journal.checks(tournament) if not ok]
        self.assertEqual(2, len(failing))

    def test_list_entry_without_details_is_ignored_and_one_with_details_waits(self):
        collector = journal.JournalCollector()
        collector.feed_record(journal_list(has_details=0))
        self.assertEqual([], collector.tournaments())

        collector.feed_record(journal_list(has_details=1))
        [pending] = collector.tournaments()
        self.assertFalse(pending.has_ranking)

    def test_map_chunks_are_not_even_decoded(self):
        collector = journal.JournalCollector()
        chunk = http_record(tbcodec.build_frame(312, 1, [[1, 2, 3]]))
        self.assertFalse(collector.feed_record(chunk))
        self.assertEqual(0, collector.packets)


class _Handler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def _reply(self, status, body):
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        _Handler.requests.append(("GET", self.path, dict(self.headers), None))
        if self.headers.get("X-Api-Token") != "cct_good":
            return self._reply(401, {"error": "This API token is invalid, expired or revoked."})
        if self.path == "/site/api/v1/me":
            return self._reply(200, {"user": {"name": "Admin"}})
        return self._reply(200, {"tournaments": [{"tournament_key": "1024:1", "name": "Rise", "rewards": []}]})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length))
        _Handler.requests.append(("POST", self.path, dict(self.headers), body))
        if not body.get("rows"):
            return self._reply(422, {"error": "The ranking was not accepted.", "errors": {"rows": "At least one player."}})
        return self._reply(201, {"event_id": 9, "event_number": 12, "event_created": True, "created": True})


class ApiClientTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}/site/"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_sends_the_token_in_both_headers(self):
        client = api.SiteClient(self.url, "cct_good", "1.0.0")
        self.assertEqual("Admin", client.me()["user"]["name"])
        self.assertEqual("Rise", client.known_tournaments()[0]["name"])
        headers = _Handler.requests[-1][2]
        self.assertEqual("Bearer cct_good", headers["Authorization"])
        self.assertEqual("cct_good", headers["X-Api-Token"])

    def test_errors_carry_status_and_reasons(self):
        with self.assertRaises(api.ApiError) as bad_token:
            api.SiteClient(self.url, "cct_bad").me()
        self.assertEqual(401, bad_token.exception.status)
        self.assertIn("invalid", bad_token.exception.describe())

        client = api.SiteClient(self.url, "cct_good")
        with self.assertRaises(api.ApiError) as invalid:
            client.send_tournament({"rows": []})
        self.assertEqual(422, invalid.exception.status)
        self.assertIn("At least one player.", invalid.exception.describe())

        self.assertEqual(12, client.send_tournament({"rows": [{"position": 1}]})["event_number"])
        self.assertEqual("/site/api/v1/tournaments", _Handler.requests[-1][1])

    def test_unreachable_site_and_missing_token(self):
        with self.assertRaises(api.ApiError) as missing:
            api.SiteClient(self.url, "").me()
        self.assertEqual(0, missing.exception.status)
        with self.assertRaises(api.ApiError):
            api.SiteClient("http://127.0.0.1:1", "cct_good").me()

    def test_site_url_is_normalised(self):
        self.assertEqual("https://kokmain.counter.li", api.normalize_site_url(" kokmain.counter.li/ "))
        self.assertEqual("http://localhost/chestcounter", api.normalize_site_url("http://localhost/chestcounter/api/v1"))


class SettingsTest(unittest.TestCase):
    def test_remembers_site_and_sent_tournaments(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            settings = Settings(path)
            settings.site_url = "https://kokmain.counter.li"
            settings.mark_sent(RESULT_UID, {"event_number": 12})

            again = Settings(path)
            self.assertEqual("https://kokmain.counter.li", again.site_url)
            self.assertEqual(12, again.sent_info(RESULT_UID)["event_number"])
            self.assertNotIn("api_token", json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
