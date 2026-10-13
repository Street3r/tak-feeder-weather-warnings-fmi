# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-weather-warnings-fmi)
# Copyright (C) 2026 Street3r (changes, see README)
"""FMI CAP warning feed (Atom) -> warning areas."""

from __future__ import annotations

import hashlib
import logging
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

LOG = logging.getLogger("tak_feeder_weather_warnings_fmi")

FEED_URL = "https://alerts.fmi.fi/cap/feed/atom_{lang}.xml"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
# All event codes of the FMI CAP profile 1.0 except uvNote (an advisory)
DEFAULT_EVENT_CODES = (
    "coldWeather,forestFireWeather,grassFireWeather,hotWeather,pedestrianSafety,rain,"
    "seaIcing,seaThunderstorm,seaWaterHeight,seaWaveHeight,seaWind,thunderstorm,"
    "trafficWeather,wind"
)
_SPECIAL = {ord("ä"): "ae", ord("ü"): "ue", ord("ö"): "oe", ord("å"): "a"}


@dataclass
class Area:
    """One warning area: a polygon of one CAP info block."""

    uid: str
    event: str
    event_code: str
    area: str
    color: str
    headline: str
    description: str
    start: datetime
    stale: datetime
    points: list[str]  # "lat,lon"
    lat: float
    lon: float


def centroid(points: list[str]) -> tuple[float, float]:
    """Polygon centroid (shoelace), falling back to the vertex mean."""
    verts = [tuple(float(v) for v in p.split(",")[:2]) for p in points]
    n = len(verts)
    area = cx = cy = 0.0
    for i in range(n):
        x0, y0 = verts[i]
        x1, y1 = verts[(i + 1) % n]
        cross = x0 * y1 - x1 * y0
        area += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(area) < 1e-12:
        return sum(v[0] for v in verts) / n, sum(v[1] for v in verts) / n
    area *= 0.5
    return cx / (6 * area), cy / (6 * area)


def _strip_ns(xml: bytes) -> ET.Element:
    root = ET.fromstring(xml)
    for el in root.iter():
        if isinstance(el.tag, str):
            el.tag = el.tag.rpartition("}")[2]
    return root


def _text(el: Optional[ET.Element], path: str) -> str:
    found = el.find(path) if el is not None else None
    return (found.text or "").strip() if found is not None else ""


def parse(xml: bytes, lang: str, urgencies: list[str], event_codes: list[str]) -> list[Area]:
    """Areas of the active warnings that pass the filters. Broken entries,
    infos or areas are skipped instead of stopping the whole update."""
    areas: list[Area] = []
    for entry in _strip_ns(xml).findall("entry"):
        try:
            areas.extend(_entry_areas(entry, lang, urgencies, event_codes))
        except (ValueError, AttributeError) as exc:
            LOG.warning("Skipping a CAP entry: %s", exc)
    return areas


def _entry_areas(entry: ET.Element, lang: str, urgencies: list[str],
                 event_codes: list[str]) -> list[Area]:
    alert = entry.find("content/alert")
    if alert is None or _text(alert, "msgType") == "Cancel":
        return []
    published = _text(entry, "published")
    identifier = "fmi-warning-" + _text(alert, "identifier")[8:]
    result: list[Area] = []
    for info in alert.findall("info"):
        if _text(info, "language") != lang:
            continue
        code = _text(info, "eventCode/value")
        if _text(info, "urgency") not in urgencies or code not in event_codes:
            continue
        color = ""  # reset per info: it must not carry over from another warning
        for param in info.findall("parameter"):
            if _text(param, "valueName") == "color":
                color = _text(param, "value")
        event = _text(info, "event")
        for area in info.findall("area"):
            desc = _text(area, "areaDesc")
            polygon = _text(area, "polygon")
            if not polygon:
                LOG.debug("Warning %s / %s has no polygon, skipped", identifier, desc)
                continue
            points = polygon.split()
            geocode = desc.replace(" ", "_").lower().translate(_SPECIAL)
            digest = hashlib.md5(f"{identifier}-{geocode}-{published}".encode()).hexdigest()
            lat, lon = centroid(points)
            result.append(Area(
                uid=str(uuid.UUID(hex=digest)),  # as in upstream
                event=event, event_code=code, area=desc, color=color,
                headline=_text(info, "headline"),
                description=_text(info, "description"),
                start=datetime.strptime(_text(info, "onset"), DATE_FORMAT),
                stale=datetime.strptime(_text(info, "expires"), DATE_FORMAT),
                points=points, lat=round(lat, 6), lon=round(lon, 6),
            ))
    return result
