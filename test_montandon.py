"""Tests for Montandon grouping, alert, and state logic."""

import importlib.util
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

spec = importlib.util.spec_from_file_location("montandon", "montandon-slack.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def record(token, family="GH01", lon=0.0, lat=0.0, days_ago=1, countries=(), storm="",
           source="USGS", level="", magnitude=None, wind_kt=None, impact=None, episode=0):
    when = NOW - timedelta(days=days_ago)
    return {
        "token": token, "source": source, "collection": f"{source.lower()}-events",
        "event_id": token.split(":")[-1], "role": "event", "episode": episode,
        "title": token, "description": "", "start": when, "end": when,
        "countries": set(countries), "family": family, "storm": storm,
        "gdacs_level": level, "magnitude": magnitude, "wind_kt": wind_kt,
        "impact": impact, "bbox": [lon, lat, lon, lat], "report_url": "",
    }


def impact(kind, value, modelled=False, people=True):
    return {"type": kind, "value": value, "modelled": modelled, "people": people}


def alert_for(*records):
    return m.summarise(m.new_group(list(records)))


class Grouping(unittest.TestCase):
    def test_sources_for_one_quake_merge(self):
        gdacs = record("GDACS:GH01:1", source="GDACS", level="orange", countries=["PAN"])
        usgs = record("USGS:GH01:us1", lon=0.5, magnitude=7.7, countries=["PAN"])
        groups = m.group_records([gdacs, usgs], {"GDACS:GH01:1", "USGS:GH01:us1"})
        self.assertEqual(len(groups), 1)

    def test_separate_gdacs_alerts_do_not_merge(self):
        # Taal and Kanlaon: two Orange eruptions about 460 km and two days apart.
        taal = record("GDACS:GH02:151", family="GH02", source="GDACS", level="orange",
                      countries=["PHL"], days_ago=3)
        kanlaon = record("GDACS:GH02:152", family="GH02", source="GDACS", level="orange",
                         countries=["PHL"], lon=3.0, days_ago=1)
        groups = m.group_records([taal, kanlaon], {taal["token"], kanlaon["token"]})
        self.assertEqual(len(groups), 2)

    def test_green_gdacs_aftershocks_join_the_main_shock(self):
        main = record("GDACS:GH01:1", source="GDACS", level="orange", magnitude=7.7)
        after = record("GDACS:GH01:2", source="GDACS", level="green", lon=0.5)
        groups = m.group_records([main, after], {main["token"], after["token"]})
        self.assertEqual(len(groups), 1)

    def test_no_chaining_through_an_intermediate_record(self):
        # B is within 500 km of both A and C, but A and C are 830 km apart.
        a = record("USGS:GH01:a", lon=0.0, magnitude=7.5)
        b = record("GLIDE:GH01:b", lon=4.0, source="GLIDE")
        c = record("USGS:GH01:c", lon=7.5, magnitude=7.0)
        groups = m.group_records([a, b, c], {"USGS:GH01:a", "USGS:GH01:c"})
        tokens = [{r["token"] for r in g["records"]} for g in groups]
        self.assertIn({"USGS:GH01:a", "GLIDE:GH01:b"}, tokens)
        self.assertIn({"USGS:GH01:c"}, tokens)

    def test_different_storm_names_do_not_merge(self):
        a = record("IBTrACS:MH03:1", family="MH03", storm="FYTIA", source="IBTrACS")
        b = record("IBTrACS:MH03:2", family="MH03", storm="GEZANI", source="IBTrACS")
        self.assertFalse(m.same_disaster(m.new_group([a]), m.new_group([b])))

    def test_different_hazards_do_not_merge(self):
        quake = record("USGS:GH01:1")
        flood = record("GDACS:MH06:1", family="MH06", source="GDACS")
        self.assertFalse(m.same_disaster(m.new_group([quake]), m.new_group([flood])))

    def test_storm_name_ignores_gdacs_year_suffix(self):
        self.assertEqual(m.storm_name("Tropical Cyclone KOGUMA-26"), "KOGUMA")
        self.assertEqual(m.storm_name("Tropical Cyclone UNNAMED"), "")


class Impacts(unittest.TestCase):
    def test_repeated_figures_take_the_maximum_not_the_sum(self):
        records = [record("GDACS:MH06:1", impact=impact("death", 1), episode=n)
                   for n in range(17)]
        self.assertEqual(m.merge_impacts(records, modelled=False), {"death": 1})

    def test_modelled_and_non_people_figures_are_kept_apart(self):
        records = [
            record("PDC:MH06:1", impact=impact("affected_total", 473000, modelled=True)),
            record("PDC:MH06:1", impact=impact("affected_total", 90000, people=False)),
            record("EM-DAT:MH06:1", impact=impact("affected_total", 1200)),
        ]
        self.assertEqual(m.merge_impacts(records, modelled=False), {"affected_total": 1200})
        self.assertEqual(m.merge_impacts(records, modelled=True), {"affected_total": 473000})


class Triggers(unittest.TestCase):
    def keys(self, *records):
        return [key for key, _ in m.triggers(alert_for(*records), NOW)]

    def test_large_quake_alerts_anywhere(self):
        self.assertEqual(self.keys(record("USGS:GH01:1", magnitude=7.1)), ["magnitude"])

    def test_gdacs_green_overrides_raw_wind(self):
        storm = dict(family="MH03", storm="KOGUMA", countries=["GUM"])
        track = record("IBTrACS:MH03:1", source="IBTrACS", wind_kt=120, **storm)
        gdacs = record("GDACS:MH03:1", source="GDACS", level="green", **storm)
        self.assertEqual(self.keys(track), ["wind"])
        self.assertEqual(self.keys(track, gdacs), [])

    def test_gdacs_orange_alerts_for_every_gdacs_hazard(self):
        for family in ("GH01", "GH02", "MH03", "MH04", "MH06", "EN02"):
            self.assertEqual(self.keys(record(f"GDACS:{family}:1", family=family,
                                              source="GDACS", level="orange")),
                             ["gdacs_orange"], family)

    def test_modelled_deaths_do_not_alert(self):
        self.assertEqual(self.keys(record("USGS:GH01:1", impact=impact("death", 500, True))), [])

    def test_old_disasters_need_a_current_gdacs_alert(self):
        old = record("EM-DAT:MH06:1", family="MH06", source="EM-DAT", days_ago=60,
                     impact=impact("death", 70))
        self.assertEqual(self.keys(old), [])


class State(unittest.TestCase):
    def test_escalation(self):
        alert = alert_for(record("GDACS:GH01:1", source="GDACS", level="orange"))
        posted = []
        self.assertEqual(m.escalation(alert, []), "new")
        m.remember(alert, [], posted)
        self.assertIsNone(m.escalation(alert, m.find_posted(alert, posted)))

        red = alert_for(record("GDACS:GH01:1", source="GDACS", level="red"))
        self.assertEqual(m.escalation(red, m.find_posted(red, posted)), "update")

        deaths = alert_for(record("GDACS:GH01:1", source="GDACS", level="orange",
                                  impact=impact("death", 120)))
        self.assertEqual(m.escalation(deaths, m.find_posted(deaths, posted)), "update")

    def test_identity_grows_so_a_disaster_is_not_reposted(self):
        a = record("GDACS:GH01:1", source="GDACS", level="orange")
        b = record("USGS:GH01:us1", magnitude=7.0)
        posted = []
        m.remember(alert_for(a), [], posted)

        # Learn B when a later run sees both sources.
        both = alert_for(a, b)
        m.remember(both, m.find_posted(both, posted), posted)
        # B still identifies the disaster after A leaves the window.
        later = alert_for(b)
        self.assertIsNone(m.escalation(later, m.find_posted(later, posted)))
        self.assertEqual(len(posted), 1)

    def test_green_gdacs_event_that_turns_orange_is_posted(self):
        main = record("GDACS:GH01:1", source="GDACS", level="orange")
        after = record("GDACS:GH01:2", source="GDACS", level="green", lon=0.5)
        posted = []
        m.remember(alert_for(main, after), [], posted)

        orange = alert_for(dict(after, gdacs_level="orange"))
        self.assertEqual(m.escalation(orange, m.find_posted(orange, posted)), "new")

    def test_alerts_unseen_for_the_retention_period_expire(self):
        old = (NOW - timedelta(days=m.STATE_RETENTION_DAYS + 1)).date().isoformat()
        recent = (NOW - timedelta(days=5)).date().isoformat()
        records = [{"tokens": ["a"], "last_seen": old},
                   {"tokens": ["b"], "last_seen": recent},
                   {"tokens": ["c"], "posted_at": recent}]
        self.assertEqual([r["tokens"] for r in m.prune(records, NOW)], [["b"], ["c"]])

    def test_state_round_trips_one_alert_per_line(self):
        records = [{"tokens": ["GDACS:GH01:1"], "title": "Séisme"}, {"tokens": ["b"]}]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.multiple(m, STATE_FILE=path, DRY_RUN=True):
                m.save_posted(records)
                self.assertEqual(m.load_posted(), records)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(f.read().splitlines()), 4)

    def test_missing_or_corrupt_state_stops_a_live_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.multiple(m, STATE_FILE=path, DRY_RUN=False, SEED_STATE=False):
                with self.assertRaises(SystemExit):
                    m.load_posted()
                with open(path, "w") as f:
                    f.write("{not json")
                with self.assertRaises(SystemExit):
                    m.load_posted()
            with mock.patch.multiple(m, STATE_FILE=os.path.join(tmp, "new.json"),
                                     DRY_RUN=False, SEED_STATE=True):
                self.assertEqual(m.load_posted(), [])
            with open(path, "w") as f:
                json.dump([{"tokens": ["x"]}], f)
            with mock.patch.multiple(m, STATE_FILE=path, DRY_RUN=False, SEED_STATE=False):
                self.assertEqual(m.load_posted(), [{"tokens": ["x"]}])


if __name__ == "__main__":
    unittest.main()
