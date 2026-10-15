# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the CAP parser, the CoT and the mission sync."""

import asyncio
import unittest
import xml.etree.ElementTree as ET
from unittest import mock
from zoneinfo import ZoneInfo

from tak_feeder_weather_warnings_fmi import cap, cot, feeder

INFO = """<info><language>en-GB</language><event>{event}</event><urgency>{urgency}</urgency>
 <eventCode><valueName>FMI</valueName><value>{code}</value></eventCode>
 <onset>2026-10-13T12:00:00+03:00</onset><expires>2026-10-13T21:00:00+03:00</expires>
 <headline>{event}</headline><description>Strong winds.</description>
 {color}{area}</info>"""
AREA = """<area><areaDesc>{name}</areaDesc>{polygon}</area>"""
POLY = "<polygon>60.0,24.0 60.0,25.0 61.0,25.0 61.0,24.0 60.0,24.0</polygon>"
COLOR = "<parameter><valueName>color</valueName><value>{}</value></parameter>"


def entry(*infos, msg="Alert", ident="urn:oid:2.49.0.1.246.0.0.2026.10.13.1"):
    return f"""<entry><published>2026-10-13T09:00:00Z</published>
 <content><alert xmlns="urn:oasis:names:tc:emergency:cap:1.2"><identifier>{ident}</identifier>
 <msgType>{msg}</msgType>{''.join(infos)}</alert></content></entry>"""


def info(event="Wind warning for land areas", code="wind", urgency="Expected",
         color="yellow", areas=(("Uusimaa", POLY),)):
    return INFO.format(event=event, code=code, urgency=urgency,
                       color=COLOR.format(color) if color else "",
                       area="".join(AREA.format(name=n, polygon=p) for n, p in areas))


def feed(*entries):
    return ('<feed xmlns="http://www.w3.org/2005/Atom">' + "".join(entries) + "</feed>").encode()


CODES = cap.DEFAULT_EVENT_CODES.split(",")


class CapTest(unittest.TestCase):
    def test_parse(self) -> None:
        (area,) = cap.parse(feed(entry(info())), "en-GB", ["Expected"], CODES)
        self.assertEqual((area.area, area.color, area.event_code), ("Uusimaa", "yellow", "wind"))
        self.assertAlmostEqual(area.lat, 60.5)
        self.assertAlmostEqual(area.lon, 24.5)

    def test_winter_codes_in_default(self) -> None:
        for code in ("trafficWeather", "pedestrianSafety", "coldWeather", "seaWaveHeight"):
            self.assertIn(code, CODES)
        self.assertNotIn("uvNote", CODES)

    def test_missing_polygon_and_color(self) -> None:
        xml = feed(entry(info(color="red"), info(color="", areas=(("A", ""), ("B", POLY)))))
        areas = cap.parse(xml, "en-GB", ["Expected"], CODES)
        self.assertEqual([(a.area, a.color) for a in areas], [("Uusimaa", "red"), ("B", "")])

    def test_filters_and_cancel(self) -> None:
        xml = feed(entry(info(urgency="Future")), entry(info(code="uvNote")),
                   entry(info(), msg="Cancel"))
        self.assertEqual(cap.parse(xml, "en-GB", ["Expected", "Immediate"], CODES), [])

    def test_broken_entry_skipped(self) -> None:
        broken = info().replace("2026-10-13T21:00:00+03:00", "soon")
        areas = cap.parse(feed(entry(broken), entry(info())), "en-GB", ["Expected"], CODES)
        self.assertEqual(len(areas), 1)


class CotTest(unittest.TestCase):
    def test_area_cot(self) -> None:
        (area,) = cap.parse(feed(entry(info())), "en-GB", ["Expected"], CODES)
        ev = ET.fromstring(cot.area_cot(area, "fmi.0001", "Weatherwarnings",
                                        ZoneInfo("Europe/Helsinki"), "Source: FMI"))
        self.assertEqual(ev.find("detail/contact").get("callsign"),
                         "FMI Wind warning for land areas Uusimaa")
        self.assertEqual(ev.find("detail/marti/dest").get("mission"), "Weatherwarnings")
        self.assertEqual(len(ev.findall("detail/link")), 5)
        text = ev.find("detail/remarks").text
        self.assertIn("Valid: 13.10. 12:00–21:00", text)  # same day: date once
        self.assertTrue(text.endswith("#weather #warning #wind"))
        self.assertEqual(ev.find("detail/fillColor").get("value"), "1090518784")
        two_days = feed(entry(info()).replace("2026-10-13T21:00", "2026-10-14T06:00"))
        (area,) = cap.parse(two_days, "en-GB", ["Expected"], CODES)
        ev = ET.fromstring(cot.area_cot(area, "fmi.0001", "W", ZoneInfo("Europe/Helsinki"), ""))
        self.assertIn("Valid: 13.10. 12:00 – 14.10. 06:00", ev.find("detail/remarks").text)


def server(status=200, uids=()):
    s = mock.Mock()
    s.mission.create_mission_subscription = mock.AsyncMock(
        return_value=(201, {"data": {"token": "t"}}))
    s.mission.get_mission = mock.AsyncMock(
        return_value=(status, {"data": [{"uids": [{"data": u} for u in uids]}]}))
    s.mission.create_mission = mock.AsyncMock(return_value=(201, {"data": [{"token": "t2"}]}))
    s.mission.remove_mission_content = mock.AsyncMock(return_value=(200, {}))
    s.mission.add_mission_package = mock.AsyncMock(return_value=(200, {}))
    return s


class SyncTest(unittest.TestCase):
    def sync(self, srv, groups=()):
        settings = feeder.Settings(api_host="tak", mission_groups=list(groups))
        package = mock.Mock(return_value=b"zip")
        return feeder.MissionSync(settings, srv, package), package

    def test_upload_and_remove(self) -> None:
        srv = server(uids=["old-uid"])
        sync, package = self.sync(srv, groups=["WEATHER", "TRAFFIC"])
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(return_value=feed(entry(info())))), \
                mock.patch.object(feeder.asyncio, "sleep", mock.AsyncMock()):
            self.assertTrue(asyncio.run(sync.run_once()))
        srv.mission.remove_mission_content.assert_awaited_once()
        self.assertEqual(package.call_args.kwargs["groups"], ["WEATHER", "TRAFFIC"])

    def test_no_change(self) -> None:
        (area,) = cap.parse(feed(entry(info())), "en-GB", ["Expected"], CODES)
        srv = server(uids=[area.uid])
        sync, _ = self.sync(srv)
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(return_value=feed(entry(info())))):
            self.assertFalse(asyncio.run(sync.run_once()))
        srv.mission.add_mission_package.assert_not_awaited()

    def stored(self, *entries):
        """The CoT TAK would return for these areas, with another event time."""
        settings = feeder.Settings()
        areas = cap.parse(feed(*entries), "en-GB", ["Expected"], CODES)
        events = "".join(
            cot.area_cot(a, settings.my_uid, settings.mission_name, ZoneInfo("Europe/Helsinki"),
                         settings.source_line).decode().replace('time="', 'time="1999')
            for a in areas)
        return [a.uid for a in areas], f'<?xml version="1.0"?><events>{events}</events>'

    def run_with_store(self, srv, stored_xml, *entries):
        srv.mission.get_mission_cot = mock.AsyncMock(return_value=(200, stored_xml))
        sync, package = self.sync(srv)
        with mock.patch.object(feeder, "fetch", mock.AsyncMock(return_value=feed(*entries))), \
                mock.patch.object(feeder.asyncio, "sleep", mock.AsyncMock()):
            return asyncio.run(sync.run_once()), package

    def test_only_new_area_sent(self) -> None:
        one = entry(info())
        two = entry(info(areas=(("Pirkanmaa", POLY),)), ident="urn:oid:2")
        uids, xml = self.stored(one)
        uploaded, package = self.run_with_store(server(uids=uids), xml, one, two)
        self.assertTrue(uploaded)
        (sent,) = package.call_args.kwargs["cot_files"]
        self.assertNotIn(sent, uids)

    def test_changed_area_sent_again(self) -> None:
        uids, xml = self.stored(entry(info()))
        xml = xml.replace("Strong winds.", "Old text.")
        uploaded, package = self.run_with_store(server(uids=uids), xml, entry(info()))
        self.assertTrue(uploaded)
        self.assertEqual(list(package.call_args.kwargs["cot_files"]), uids)

    def test_unchanged_store_nothing_sent(self) -> None:
        uids, xml = self.stored(entry(info()))
        uploaded, package = self.run_with_store(server(uids=uids), xml, entry(info()))
        self.assertFalse(uploaded)
        package.assert_not_called()

    def test_mission_created_with_groups(self) -> None:
        srv = server(status=404)
        sync, _ = self.sync(srv, groups=["WEATHER"])
        asyncio.run(sync.run_once())
        self.assertEqual(srv.mission.create_mission.call_args.kwargs["group"], ["WEATHER"])

    def test_error_status_raises_instead_of_spinning(self) -> None:
        sync, _ = self.sync(server(status=500))
        with self.assertRaises(RuntimeError):
            asyncio.run(sync.run_once())


if __name__ == "__main__":
    unittest.main()
