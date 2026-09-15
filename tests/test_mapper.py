"""The Journal mapper against a simulated game.

The fake game is a small state machine: the list of cards, an open card, the
ranking. Clicks at the calibrated points move it between those screens, and
opening a ranking makes it "send" the ranking message the way the real game
does, through the same JournalCollector the mapper listens to.
"""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import journal  # noqa: E402
import mapper  # noqa: E402
import tbcodec  # noqa: E402
import titles  # noqa: E402
from calibration import Calibration  # noqa: E402
from test_journal import ws_record  # noqa: E402

PITCH = 90


def ranking_record(uid, key):
    inner = tbcodec.pack(["global_tournament_user_result", list(key), 1, [[1, 1, [[[1001], 50], [[1002], 10]], [], []]]])
    return ws_record([[{"1": b"\x93\x06\x00\xa0"}], [{uid: [inner, True]}]], idx=7)


def make_calibration(path):
    cal = Calibration(path)
    cal.set_viewport(1000, 800)
    cal.set_point("journal_button", 50, 750)
    cal.set_region("card1_title", (300, 100), (800, 130))
    cal.set_region("card2_title", (300, 100 + PITCH), (800, 130 + PITCH))
    cal.set_region("card_icon", (220, 95), (280, 150))
    cal.set_point("next_page", 700, 700)
    cal.set_point("card_open", 500, 115)
    screen = np.random.default_rng(1).integers(0, 255, (800, 1000, 3), dtype=np.uint8)
    cal.set_region("show_details", (600, 600), (760, 640), screen)
    cal.set_point("details_close", 900, 60)
    cal.set_point("card_close", 950, 40)
    cal.cards_per_page = 3
    cal.use_viewport(1000, 800)
    return cal


class FakeGame:
    """Plays the browser: captures return which text is on screen, clicks move screens."""

    def __init__(self, calibration, pages, answers, collector):
        self.cal = calibration
        self.pages = pages          # list of pages, each a list of card titles
        self.answers = answers      # tournament name -> (result uid, (type, variant)), None when cached
        self.collector = collector
        self.page = 0
        self.view = "list"
        self.open_card = None
        self.texts = []
        self.clicks = []
        self.stuck_on_close = False

    # browser API used by the mapper
    connected = True

    def viewport(self):
        return 1000, 800

    def focus_tab(self):
        pass

    def _image(self, text):
        self.texts.append(text)
        return np.zeros((4, len(self.texts), 3), dtype=np.uint8)

    def capture(self, region=None, scale=1.0):
        for i in range(self.cal.cards_per_page):
            if region == self.cal.card_title_region(i):
                visible = self.view == "list" and i < len(self.pages[self.page])
                return self._image(self.pages[self.page][i] if visible else "")
        if region == self.cal.card_icon_region(0) or region in [self.cal.card_icon_region(i) for i in range(3)]:
            return np.full((20, 20, 3), 90, dtype=np.uint8)
        return self._image(f"screen {self.page} {self.view}")

    def click(self, x, y, delay=0.0):
        self.clicks.append((x, y))
        for i in range(self.cal.cards_per_page):
            if (x, y) == self.cal.card_click_point(i) and self.view == "list":
                self.view, self.open_card = "card", i
                return
        if (x, y) == self.cal.region_center("show_details") and self.view == "card":
            self.view = "details"
            name = titles.extract_tournament_name(self.pages[self.page][self.open_card])
            answer = self.answers.get(name)
            if answer is not None:
                self.collector.feed_record(ranking_record(*answer))
            return
        if (x, y) == self.cal.point("details_close") and self.view == "details":
            self.view = "card"
            return
        if (x, y) == self.cal.point("card_close") and self.view == "card" and not self.stuck_on_close:
            self.view = "list"
            return
        if (x, y) == self.cal.point("next_page") and self.page < len(self.pages) - 1:
            self.page += 1

    def press_key(self, name, delay=0.0):
        if self.view == "details":
            self.view = "card"
        elif self.view == "card" and not self.stuck_on_close:
            self.view = "list"


class FakeOcr:
    def __init__(self, game):
        self.game = game
        self.error = ""

    def available(self):
        return True

    def read(self, image, single_line=False):
        if image is None or image.shape[0] != 4:
            return ""
        return self.game.texts[image.shape[1] - 1]


def title(name):
    return f"Your Clanmates' results in {name}"


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cal = make_calibration(os.path.join(self.tmp.name, "cal.json"))
        self.collector = journal.JournalCollector()

    def tearDown(self):
        self.tmp.cleanup()

    def run_mapper(self, game, **kwargs):
        options = mapper.MapperOptions(max_pages=10, step_delay=0.0, answer_timeout=0.3)
        runner = mapper.JournalMapper(game, self.cal, FakeOcr(game), self.collector, options, **kwargs)
        return runner.run()

    def test_maps_every_tournament_across_pages_and_skips_the_rest(self):
        pages = [
            [title("Rise of the Ancients"), "Clan treasury purchase", title("Epic Hunt")],
            [title("Rise of the Ancients"), title("Known One")],
        ]
        answers = {
            "Rise of the Ancients": ("aaaaaaaa-0000-4000-8000-000000000001", (1024, 1)),
            "Epic Hunt": ("aaaaaaaa-0000-4000-8000-000000000002", (1007, 14)),
            "Known One": ("aaaaaaaa-0000-4000-8000-000000000003", (1001, 1)),
        }
        game = FakeGame(self.cal, pages, answers, self.collector)

        cards = self.run_mapper(game, known_names={"known one"})

        by_name = {(c.name, c.page): c for c in cards}
        rise = by_name[("Rise of the Ancients", 1)]
        self.assertEqual(mapper.STATUS_MAPPED, rise.status)
        self.assertEqual("1024:1", rise.tournament_key)
        self.assertEqual(1024, rise.game_type)
        self.assertIsNotNone(rise.icon_png)
        self.assertEqual("1007:14", by_name[("Epic Hunt", 1)].tournament_key)
        # Mapped on page 1, so not opened again on page 2; known on the site, not opened at all.
        self.assertEqual(mapper.STATUS_SKIPPED, by_name[("Rise of the Ancients", 2)].status)
        self.assertEqual(mapper.STATUS_SKIPPED, by_name[("Known One", 2)].status)
        self.assertEqual("list", game.view)
        self.assertEqual(1, game.page)

        entries = mapper.catalogue_entries(cards)
        self.assertEqual({"Rise of the Ancients", "Epic Hunt"}, {e["name"] for e in entries})
        self.assertTrue(all("image" in e for e in entries))
        self.assertEqual([], mapper.conflicts(cards))

    def test_a_cached_ranking_is_reported_and_the_walk_goes_on(self):
        pages = [[title("Cached"), title("Fresh")]]
        answers = {"Fresh": ("bbbbbbbb-0000-4000-8000-000000000001", (1013, 4))}
        game = FakeGame(self.cal, pages, answers, self.collector)

        cards = self.run_mapper(game)

        self.assertEqual([mapper.STATUS_NO_ANSWER, mapper.STATUS_MAPPED], [c.status for c in cards])
        self.assertEqual(["Fresh"], [e["name"] for e in mapper.catalogue_entries(cards)])

    def test_losing_the_list_stops_with_a_clear_message(self):
        game = FakeGame(self.cal, [[title("Stuck")]], {"Stuck": ("cccccccc-0000-4000-8000-000000000001", (1, 1))}, self.collector)
        game.stuck_on_close = True

        with self.assertRaises(RuntimeError) as failure:
            self.run_mapper(game)
        self.assertIn("não voltou para a lista", str(failure.exception))

    def test_incomplete_calibration_is_refused(self):
        self.cal.forget("next_page")
        game = FakeGame(self.cal, [[title("A")]], {}, self.collector)
        with self.assertRaises(RuntimeError):
            self.run_mapper(game)

    def test_conflicting_readings_are_reported(self):
        cards = [
            mapper.MappedCard("Rise", 1, 1, mapper.STATUS_MAPPED, "u1", "1024:1"),
            mapper.MappedCard("Rlse", 2, 1, mapper.STATUS_MAPPED, "u2", "1024:3"),
            mapper.MappedCard("Hunt", 2, 2, mapper.STATUS_MAPPED, "u3", "1007:1"),
            mapper.MappedCard("hunt", 3, 1, mapper.STATUS_MAPPED, "u4", "1010:1"),
        ]
        problems = mapper.conflicts(cards)
        self.assertEqual(2, len(problems))
        self.assertIn("1024", problems[0])


class CalibrationTest(unittest.TestCase):
    def test_cards_are_derived_from_the_first_two_and_rescale_with_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            cal = make_calibration(os.path.join(tmp, "cal.json"))
            cal.save()
            again = Calibration(os.path.join(tmp, "cal.json"))
            again.use_viewport(1000, 800)
            self.assertEqual(3, again.cards_per_page)
            self.assertEqual((300, 100 + 2 * PITCH, 500, 30), again.card_title_region(2))
            self.assertEqual((500, 115 + PITCH), again.card_click_point(1))
            self.assertEqual([], again.missing_steps())

            again.use_viewport(2000, 1600)
            self.assertEqual((1000, 230 + 2 * 180), again.card_click_point(2))


if __name__ == "__main__":
    unittest.main()
