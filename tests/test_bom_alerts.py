import copy
import datetime as dt
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bom_alerts  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def parsed(name):
    return bom_alerts.parse_product(fixture(name))


def read(path):
    with open(path) as f:
        return f.read()


def with_phase(record, phase):
    r = copy.deepcopy(record)
    r["phase"] = phase
    return r


def stored(record):
    active = {}
    bom_alerts.apply_event(active, "start", record, "2026-10-01T00:00:00Z")
    return active


class ParseTests(unittest.TestCase):
    def test_marine_wind_summary(self):
        w = parsed("IDV20600.xml")
        self.assertEqual(w["id"], "IDV20600")
        self.assertEqual(w["state"], "VIC")
        self.assertEqual(w["title"], "Marine Wind Warning Summary for Victoria")
        self.assertEqual(w["service"], "WSM")
        self.assertEqual(w["phase"], "UPD")
        self.assertEqual(w["issued"], "2026-10-10T05:15:00Z")
        self.assertIn("Strong Wind Warning", w["hazards"])
        self.assertIn("West Coast: SA-VIC Border to Cape Otway", w["areas"])
        # Cancelled areas are not counted as warned areas.
        self.assertNotIn(
            "East Gippsland Coast: Lakes Entrance to 60nm east of Gabo Island", w["areas"]
        )

    def test_national_high_seas_warning(self):
        w = parsed("IDY21050.xml")
        self.assertEqual(w["state"], "AUS")
        self.assertTrue(w["title"].startswith("GALE WARNING"))
        self.assertEqual(w["phase"], "NEW")

    def test_cancelled_warning(self):
        self.assertEqual(parsed("IDN20400.xml")["phase"], "CAN")

    def test_forecast_is_not_a_warning(self):
        self.assertIsNone(parsed("IDW10700.xml"))


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.vic = parsed("IDV20600.xml")
        self.gale = parsed("IDY21050.xml")
        self.listed = {"IDV20600", "IDY21050", "IDW10700"}

    def test_new_warnings_start(self):
        events = bom_alerts.diff({}, self.listed, {"IDV20600": self.vic, "IDY21050": self.gale})
        self.assertEqual([(k, r["id"]) for k, r, _ in events],
                         [("start", "IDV20600"), ("start", "IDY21050")])

    def test_update_of_active_warning_is_not_an_event(self):
        active = stored(self.vic)
        reissued = dict(self.vic, issued="2026-10-10T11:00:00Z", areas=["Somewhere else"])
        self.assertEqual(bom_alerts.diff(active, self.listed, {"IDV20600": reissued}), [])

    def test_disappeared_warning_ends(self):
        active = stored(self.vic)
        events = bom_alerts.diff(active, {"IDW10700"}, {})
        self.assertEqual([(k, r["id"], why) for k, r, why in events],
                         [("end", "IDV20600", "removed")])

    def test_cancelled_warning_ends(self):
        active = stored(self.vic)
        events = bom_alerts.diff(active, self.listed, {"IDV20600": with_phase(self.vic, "CAN")})
        self.assertEqual([(k, why) for k, _, why in events], [("end", "cancelled")])

    def test_cancelled_warning_never_seen_does_not_start(self):
        can = parsed("IDN20400.xml")
        self.assertEqual(bom_alerts.diff({}, {"IDN20400"}, {"IDN20400": can}), [])

    def test_unfetchable_warning_is_left_alone(self):
        active = stored(self.vic)
        self.assertEqual(bom_alerts.diff(active, self.listed, {}, unknown_ids={"IDV20600"}), [])

    def test_ends_come_before_starts(self):
        active = stored(self.vic)
        events = bom_alerts.diff(active, {"IDY21050"}, {"IDY21050": self.gale})
        self.assertEqual([k for k, _, _ in events], ["end", "start"])

    def test_commit_messages(self):
        self.assertEqual(
            bom_alerts.commit_message("start", self.vic),
            "Start: Marine Wind Warning Summary for Victoria (VIC) [IDV20600]",
        )
        self.assertEqual(
            bom_alerts.commit_message("end", self.vic),
            "End: Marine Wind Warning Summary for Victoria (VIC) [IDV20600]",
        )


class StorageTests(unittest.TestCase):
    def test_stored_record_has_no_phase_and_is_stable(self):
        vic = parsed("IDV20600.xml")
        active = stored(vic)
        self.assertNotIn("phase", active["IDV20600"])
        self.assertEqual(active["IDV20600"]["started"], "2026-10-01T00:00:00Z")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "data", "active.json")
            bom_alerts.save_active(path, active)
            first = read(path)
            bom_alerts.save_active(path, bom_alerts.load_active(path))
            self.assertEqual(read(path), first)

    def test_log_appends_monthly_jsonl(self):
        vic = parsed("IDV20600.xml")
        when = dt.datetime(2026, 10, 10, 9, 30, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as d:
            e1 = bom_alerts.event_entry("start", vic, None, "2026-10-10T09:30:00Z", {})
            e2 = bom_alerts.event_entry("end", vic, "removed", "2026-10-10T21:30:00Z", {})
            path = bom_alerts.append_log(d, e1, when)
            bom_alerts.append_log(d, e2, when)
            self.assertEqual(path, os.path.join(d, "2026", "10.jsonl"))
            lines = [json.loads(line) for line in read(path).splitlines()]
            self.assertEqual([e["event"] for e in lines], ["start", "end"])
            self.assertEqual(lines[1]["reason"], "removed")


if __name__ == "__main__":
    unittest.main()
