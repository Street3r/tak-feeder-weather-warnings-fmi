# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-weather-warnings-fmi)
# Copyright (C) 2026 Street3r (changes, see README)
"""CoT for warning polygons and the FMI keep-alive contact."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cap import Area
from .common import cot_time, remarks_text

# Semi-transparent fills, as in upstream
COLORS = {"yellow": "1090518784", "orange": "1090483968", "red": "1090453504"}


def area_cot(area: Area, my_uid: str, mission: str, tz: ZoneInfo, source_line: str) -> bytes:
    """Polygon of one warning area (u-d-f), routed to the mission."""
    root = ET.Element("event", {
        "version": "2.0", "type": "u-d-f", "uid": area.uid, "how": "h-e",
        "time": cot_time(), "start": cot_time(area.start), "stale": cot_time(area.stale),
    })
    ET.SubElement(root, "point", {"lat": str(area.lat), "lon": str(area.lon), "hae": "0",
                                  "ce": "9999999.0", "le": "9999999.0"})
    detail = ET.SubElement(root, "detail")
    marti = ET.SubElement(detail, "marti")
    ET.SubElement(marti, "dest", {"mission": mission})
    ET.SubElement(detail, "contact", {"callsign": f"FMI {area.event} {area.area}".strip()})
    valid = (f"Valid: {area.start.astimezone(tz).strftime('%d.%m. %H:%M')} – "
             f"{area.stale.astimezone(tz).strftime('%d.%m. %H:%M')}")
    lines = [area.headline, area.description,
             f"Level: {area.color}" if area.color else None, valid]
    ET.SubElement(detail, "remarks").text = remarks_text(
        lines, source_line, ("weather", "warning", area.event_code))
    ET.SubElement(detail, "creator", {"uid": my_uid, "callsign": "FMI", "type": "a-f-G-I-U-R",
                                      "time": cot_time(area.start)})
    argb = COLORS.get(area.color.lower(), "-1")
    for tag, value in (("color", argb), ("labels_on", "false"), ("strokeColor", argb),
                       ("strokeWeight", "1"), ("strokeStyle", "solid"), ("fillColor", argb)):
        ET.SubElement(detail, tag, {"value": value})
    for point in area.points:
        ET.SubElement(detail, "link", {"point": point})
    return ET.tostring(root)


def keep_alive(uid: str, version: str) -> bytes:
    """The FMI contact that owns the mission (as in upstream)."""
    now = datetime.now(timezone.utc)
    root = ET.Element("event", {
        "version": "2.0", "type": "a-f-G-U", "uid": uid, "how": "h-e",
        "time": cot_time(now), "start": cot_time(now),
        "stale": cot_time(now + timedelta(seconds=60)),
    })
    ET.SubElement(root, "point", {"lat": "0.0", "lon": "0.0", "hae": "9999999.0",
                                  "ce": "9999999", "le": "9999999"})
    detail = ET.SubElement(root, "detail")
    ET.SubElement(detail, "takv", {"device": "Docker", "os": "Linux",
                                   "platform": "Warning feeder", "version": version})
    ET.SubElement(detail, "contact", {"callsign": "FMI", "phone": "+358-29-539-1000",
                                      "emailAddress": "kirjaamo@fmi.fi",
                                      "endpoint": "*:-1:stcp"})
    ET.SubElement(detail, "uid", {"Droid": "FMI"})
    ET.SubElement(detail, "__group", {"name": "White", "role": "HQ"})
    ET.SubElement(detail, "status", {"battery": "99"})
    ET.SubElement(detail, "track", {"course": "0", "speed": "0"})
    ET.SubElement(detail, "remarks").text = "#weather"
    return ET.tostring(root)
