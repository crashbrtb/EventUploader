"""Testes da descoberta com pacotes montados no dialeto do jogo.

Rodar:  .venv\\Scripts\\python -m unittest discover -s tests -v
"""
import base64
import struct
import os
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import finder  # noqa: E402
import tbcodec  # noqa: E402

NAMES = ["Baǧ do minotauro", "Brunilda", "Lord Piero80"] + [f"Jogador {i:03d}" for i in range(4, 101)]


def points_for(position: int) -> int:
    # Pontos na casa dos bilhoes, acima de 2^32, estritamente decrescentes.
    return 9_000_000_000 - position * 37_123_457


def record(data: bytes, idx: int = 1, url: str = "https://game-us1.totalbattle.com/rubens-realm1") -> dict:
    return {
        "idx": idx, "kind": "RESP", "ts": float(idx), "url": url, "size": len(data),
        "routes": [f.route for f in tbcodec.iter_frames(data) if f.route is not None],
        "b64": base64.b64encode(data).decode("ascii"),
    }


def batch(*frames: bytes) -> bytes:
    body = tbcodec.pack([2, 1]) + tbcodec.pack(len(frames)) + b"".join(frames)
    total = 8 + len(body)
    return struct.pack("<II", total, total) + body


class RecordStyleTest(unittest.TestCase):
    def setUp(self):
        finder.clear_cache()
        rows = [
            [100000 + p, "K237", NAMES[p - 1], 2_000_000_000 - p * 1000, points_for(p), p]
            for p in range(1, 101)
        ]
        # O proprio jogador aparece noutro lugar com os mesmos pontos do 2o
        # colocado: a descoberta nao pode se confundir com isso.
        ranking = tbcodec.build_frame(777, 9, {"event": 55}, rows, {"meu": [NAMES[1], points_for(2)]})
        heartbeat = tbcodec.build_frame(318, 8, [1, 2, 3])
        self.rec = record(batch(heartbeat, ranking), idx=3)
        self.noise = record(tbcodec.build_frame(312, 1, [[1, 2, 3], ["Brunilda do outro reino", 5]]), idx=2)
        self.targets = [
            finder.Target(name="Baǧ do minotauro", points=points_for(1), position=1, power=2_000_000_000 - 1000),
            finder.Target(name="brunilda", points=finder.parse_int(f"{points_for(2):,}"), position=2,
                          power=2_000_000_000 - 2000),
            finder.Target(name="Jogador 100", points=points_for(100), position=100, power=2_000_000_000 - 100000),
        ]

    def test_extracts_all_hundred_players(self):
        candidates = finder.search([self.noise, self.rec], self.targets)
        self.assertEqual(candidates[0].record["idx"], 3)
        self.assertEqual(candidates[0].full, 3)

        tpl, msg = finder.infer_template(finder.tree_of(self.rec), self.targets, candidates[0].hits)
        self.assertIsNotNone(tpl, msg)
        self.assertEqual(tpl.style, "registro")
        self.assertEqual(tpl.position_suffix, (5,))
        self.assertEqual(tpl.power_suffix, (3,))

        rows = finder.extract(finder.tree_of(self.rec), tpl)
        self.assertEqual(len(rows), 100)
        self.assertEqual(rows[0].name, "Baǧ do minotauro")
        self.assertEqual(rows[99].points, points_for(100))
        self.assertTrue(all(ok for ok, _ in finder.check(rows, self.targets)), finder.check(rows, self.targets))

        self.assertTrue(any("linha[2]" in line for line in finder.columns_sample(finder.tree_of(self.rec), tpl)))

    def test_needs_two_players(self):
        candidates = finder.search([self.rec], self.targets[:1])
        tpl, msg = finder.infer_template(finder.tree_of(self.rec), self.targets[:1], candidates[0].hits)
        self.assertIsNone(tpl)
        self.assertIn("2 jogadores", msg)

    def test_report_zip(self):
        candidates = finder.search([self.rec], self.targets)
        tpl, _ = finder.infer_template(finder.tree_of(self.rec), self.targets, candidates[0].hits)
        rows = finder.extract(finder.tree_of(self.rec), tpl)
        with tempfile.TemporaryDirectory() as tmp:
            path = finder.export_report(tmp, self.targets, [self.rec], candidates, candidates[0], tpl, rows,
                                        finder.check(rows, self.targets))
            with zipfile.ZipFile(path) as zf:
                self.assertEqual(sorted(zf.namelist()),
                                 ["pacotes_candidatos.jsonl", "ranking.csv", "relatorio.json"])


class OtherShapesTest(unittest.TestCase):
    def setUp(self):
        finder.clear_cache()

    def test_parallel_lists_in_json_without_position(self):
        import json
        body = json.dumps({
            "names": NAMES[:30],
            "scores": [str(points_for(p)) for p in range(1, 31)],
        }).encode("utf-8")
        rec = record(body)
        targets = [
            finder.Target("Brunilda", points_for(2), position=2),
            finder.Target("Jogador 030", points_for(30), position=30),
        ]
        cand = finder.search([rec], targets)[0]
        tpl, msg = finder.infer_template(finder.tree_of(rec), targets, cand.hits)
        self.assertIsNotNone(tpl, msg)
        self.assertEqual(tpl.style, "listas paralelas")
        self.assertEqual(tpl.position_index_base, 1)
        rows = finder.extract(finder.tree_of(rec), tpl)
        self.assertEqual([r.position for r in rows], list(range(1, 31)))

    def test_map_keyed_by_player_id(self):
        table = {str(5000 + p): {"n": NAMES[p - 1], "s": points_for(p), "r": p} for p in range(1, 21)}
        rec = record(tbcodec.build_frame(901, 1, table))
        targets = [
            finder.Target("Lord Piero80", points_for(3), position=3),
            finder.Target("Jogador 015", points_for(15), position=15),
        ]
        cand = finder.search([rec], targets)[0]
        tpl, msg = finder.infer_template(finder.tree_of(rec), targets, cand.hits)
        self.assertIsNotNone(tpl, msg)
        self.assertEqual(tpl.position_suffix, ("r",))
        rows = finder.extract(finder.tree_of(rec), tpl)
        self.assertEqual(len(rows), 20)
        self.assertTrue(all(ok for ok, _ in finder.check(rows, targets)))

    def test_paginated_ranking_is_merged(self):
        def page(start, seq):
            rows = [[NAMES[p - 1], points_for(p), p] for p in range(start, start + 50)]
            return tbcodec.build_frame(777, seq, rows)

        first = record(page(1, 1), idx=1)
        second = record(page(51, 2), idx=2)
        targets = [
            finder.Target("Brunilda", points_for(2), position=2),
            finder.Target("Jogador 040", points_for(40), position=40),
        ]
        cand = finder.search([first, second], targets)[0]
        tpl, msg = finder.infer_template(finder.tree_of(cand.record), targets, cand.hits)
        self.assertIsNotNone(tpl, msg)
        rows, used = finder.extract_everywhere([first, second], cand.record, tpl)
        self.assertEqual(len(rows), 100)
        self.assertEqual(used, [1, 2])
        self.assertTrue(all(ok for ok, _ in finder.check(rows, targets)))

    def test_ranking_inside_event_tail(self):
        rows = [[NAMES[p - 1], points_for(p), p] for p in range(1, 11)]
        data = tbcodec.build_frame(318, 4, [0], tail_events=([4001, rows],))
        rec = record(data)
        targets = [finder.Target("Brunilda", points_for(2)), finder.Target("Jogador 009", points_for(9))]
        cand = finder.search([rec], targets)[0]
        tpl, msg = finder.infer_template(finder.tree_of(rec), targets, cand.hits)
        self.assertIsNotNone(tpl, msg)
        self.assertEqual(len(finder.extract(finder.tree_of(rec), tpl)), 10)


class JournalJoinTest(unittest.TestCase):
    """Formato visto na gravacao real (12/09/2026).

    - WebSocket, msgpack LE sem enquadramento:
      `[[{canal: bin}], [{uuid: [bin<[tipo, [evento, 1], n, [[1, 1, [[[id], pontos], ...]]]]>]}]]`
    - Perfis numa resposta HTTP, rota 402: `[[id], conta, nome, pais, ..., poder em [10], ...]`
    """

    def setUp(self):
        finder.clear_cache()
        self.count = 98
        ids = [300647954111 + p * 7919 for p in range(1, self.count + 1)]
        self.ids = ids
        ranking = [[[ids[p - 1]], points_for(p) if p < 95 else 0] for p in range(1, self.count + 1)]
        inner = tbcodec.pack(["global_tournament_user_result", [1024, 1], 3, [[1, 1, ranking]]])
        ws = tbcodec.pack([{"733765024": b"\x93\xcd\x06\x02\x00\xa0"}]) + tbcodec.pack(
            [{"eede1d1c-3f13-481a-819d-51fdcebb1e54": [inner, True]}]
        )
        self.ws = {**record(b"", idx=743, url="wss://websocket"), "kind": "WS_IN",
                   "b64": base64.b64encode(ws).decode("ascii"), "routes": []}

        def profile(p):
            return [[ids[p - 1]], f"tb:{p}", NAMES[p - 1], "BR", 3, 1, 2, 444, 45, 25, 2_000_000_000 - p, [44109], 528144, "KOK"]

        # Os perfis chegam em dois pacotes, como na gravacao real: o primeiro
        # nao tem todo mundo.
        first = [profile(p) for p in range(1, 60)]
        second = [profile(p) for p in range(40, self.count + 1)] + [[[999999999], "x", "Outro Clan", "US", 0, 0, 0, 0, 0, 0, 1]]
        self.profiles_a = record(tbcodec.build_frame(402, 1, [first]), idx=588)
        self.profiles_b = record(batch(tbcodec.build_frame(402, 2, [second]), tbcodec.build_frame(24500, 3, [[1]])), idx=600)
        self.records = [self.profiles_a, self.ws, self.profiles_b]
        self.targets = [
            finder.Target("Baǧ do minotauro", points_for(1), position=1, power=2_000_000_000 - 1),
            finder.Target("Jogador 047", points_for(47), position=47, power=2_000_000_000 - 47),
            finder.Target("Jogador 098", 0, position=98),  # ultimo colocado com 0 pontos
        ]

    def test_websocket_blob_is_decoded(self):
        tree = finder.tree_of(self.ws)
        self.assertIn("stream", tree)
        blob = finder.get_path(tree, ("stream", 1, 0, "eede1d1c-3f13-481a-819d-51fdcebb1e54", 0, "blob", 0))
        self.assertEqual(blob[0], "global_tournament_user_result")

    def test_ranking_by_id_gets_names_from_profiles(self):
        candidates = finder.search(self.records, self.targets)
        start = finder.best_names_and_points(candidates)
        self.assertEqual(start.record["idx"], 743)

        tpl, msg = finder.infer_template(finder.tree_of(start.record), self.targets, start.hits)
        self.assertIsNone(tpl)
        join, msg = finder.find_join(candidates, start, self.targets)
        self.assertIsNotNone(join, msg)
        self.assertEqual(join.points_id_suffix, (0, 0))
        self.assertEqual(join.names_id_suffix, (0, 0))
        self.assertEqual(join.names_suffix, (2,))
        self.assertEqual(join.names_route, 402)
        self.assertEqual(join.position_index_base, 1)
        self.assertEqual(join.power_suffix, (10,))

        rows = finder.extract_join(self.records, join)
        self.assertEqual(len(rows), self.count)
        self.assertEqual(rows[46].name, "Jogador 047")
        self.assertEqual(rows[97].name, "Jogador 098")
        self.assertEqual(rows[0].power, 2_000_000_000 - 1)
        checks = finder.check(rows, self.targets)
        self.assertTrue(all(ok for ok, _ in checks), checks)

    def test_missing_profile_is_reported(self):
        candidates = finder.search([self.profiles_a, self.ws], self.targets)
        start = finder.best_names_and_points(candidates)
        join, msg = finder.find_join(candidates, start, self.targets[:2])
        self.assertIsNotNone(join, msg)
        rows = finder.extract_join([self.profiles_a, self.ws], join)
        unknown = [r for r in rows if r.name.startswith(finder.UNKNOWN_NAME_PREFIX)]
        self.assertEqual(len(unknown), self.count - 59)
        self.assertIn((False, f"{len(unknown)} jogador(es) sem nome: o perfil nao veio na gravacao "
                              "(abra o perfil dele ou role a lista de membros do clan durante a gravacao)"),
                      finder.check(rows, self.targets[:2]))


class ParsingTest(unittest.TestCase):
    def test_parse_int(self):
        self.assertEqual(finder.parse_int("2,010,420,872 points"), 2010420872)
        self.assertEqual(finder.parse_int("2.010.420.872"), 2010420872)
        self.assertIsNone(finder.parse_int(""))

    def test_as_int_ignores_bool_and_text(self):
        self.assertIsNone(finder.as_int(True))
        self.assertIsNone(finder.as_int("K237"))
        self.assertEqual(finder.as_int("1 234"), 1234)


if __name__ == "__main__":
    unittest.main()
