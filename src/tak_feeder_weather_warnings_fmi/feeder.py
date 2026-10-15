# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-weather-warnings-fmi)
# Copyright (C) 2026 Street3r (changes, see README)
"""Keeps a TAK mission in sync with FMI's weather warnings."""

from __future__ import annotations

import asyncio
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple
from zoneinfo import ZoneInfo

from . import __version__, cap, cot
from .common import Health, env, env_float, env_list, fetch

LOG = logging.getLogger("tak_feeder_weather_warnings_fmi")


@dataclass
class Settings:
    """Feeder settings from the environment (see .env.example)."""

    api_host: str = ""
    cert: str = ""
    key: str = ""
    my_uid: str = "fmi.0001"
    lang: str = "en-GB"
    mission_name: str = "Weatherwarnings"
    mission_groups: list[str] = field(default_factory=list)
    mission_token: str = ""
    update_interval: float = 3600.0
    retry_interval: float = 300.0
    urgencies: list[str] = field(default_factory=lambda: ["Expected", "Immediate"])
    event_codes: list[str] = field(
        default_factory=lambda: cap.DEFAULT_EVENT_CODES.split(","))
    timezone: str = "Europe/Helsinki"
    source_line: str = "Source: Finnish Meteorological Institute open data, CC BY 4.0"

    @classmethod
    def from_env(cls) -> "Settings":
        """Read settings."""
        d = cls()
        return cls(
            api_host=env("API_HOST", ""),
            cert=env("PYTAK_TLS_CLIENT_CERT", ""),
            key=env("PYTAK_TLS_CLIENT_KEY", ""),
            my_uid=env("MY_UID", d.my_uid),
            lang=env("FMI_LANG", d.lang),
            mission_name=env("MISSION_NAME", d.mission_name),
            mission_groups=env_list("MISSION_GROUP", "", "MISSION_GROUPS"),
            mission_token=env("MISSION_TOKEN", ""),
            update_interval=env_float("UPDATE_INTERVAL", d.update_interval),
            retry_interval=env_float("RETRY_INTERVAL", d.retry_interval),
            urgencies=env_list("FILTER_URGENCY", ",".join(d.urgencies)),
            event_codes=env_list("FILTER_EVENTCODE", cap.DEFAULT_EVENT_CODES),
            timezone=env("TIMEZONE", d.timezone),
            source_line=env("SOURCE_LINE", d.source_line),
        )


def cot_signature(event: ET.Element) -> Tuple[Any, ...]:
    """What ATAK shows of a warning area: type, label, remarks, colours and
    polygon. Times are left out, the event time changes every round."""
    detail = event.find("detail")

    def attr(path: str, name: str) -> str:
        el = detail.find(path) if detail is not None else None
        return el.get(name, "") if el is not None else ""

    remarks = detail.find("remarks") if detail is not None else None
    links = detail.findall("link") if detail is not None else []
    return (
        event.get("type", ""),
        attr("contact", "callsign"),
        (remarks.text or "").strip() if remarks is not None else "",
        attr("color", "value"),
        attr("fillColor", "value"),
        tuple(link.get("point", "").replace(" ", "") for link in links),
    )


def parse_signatures(xml: Any) -> Optional[Dict[str, Tuple[Any, ...]]]:
    """uid -> signature of the CoT items TAK stores for the mission, None if
    the response cannot be read."""
    if isinstance(xml, bytes):
        xml = xml.decode("utf-8", "replace")
    if not isinstance(xml, str) or not xml.strip():
        return None
    try:
        root = ET.fromstring(xml.strip())
    except ET.ParseError:
        return None
    return {e.get("uid", ""): cot_signature(e) for e in root.iter("event")}


class MissionSync:
    """One sync round: get or create the mission, remove expired warnings,
    upload a mission package with the new or changed warning areas."""

    def __init__(self, settings: Settings, server: Any, build_package: Any) -> None:
        self.s = settings
        self.server = server
        self.build_package = build_package
        self.token = settings.mission_token
        self.tz = ZoneInfo(settings.timezone)
        self.headers = {"User-Agent": f"tak-feeder-weather-warnings-fmi/{__version__}"}

    async def _create(self) -> None:
        group: Any = self.s.mission_groups or ""  # a list becomes repeated group=
        status, result = await self.server.mission.create_mission(
            self.s.mission_name, self.s.my_uid, group=group,
            default_role="MISSION_READONLY_SUBSCRIBER", classification="unclassified")
        if status >= 400:
            raise RuntimeError(f"cannot create mission: HTTP {status} {result}")
        self.token = result["data"][0]["token"]
        LOG.info("Mission %s created", self.s.mission_name)

    async def subscribe(self) -> None:
        """Get a mission token: subscribe, or create the mission."""
        if self.token:
            return
        status, result = await self.server.mission.create_mission_subscription(
            self.s.mission_name, self.s.my_uid)
        if status == 201:
            self.token = result["data"]["token"]
            LOG.info("Subscribed to mission %s", self.s.mission_name)
        elif status == 404:
            await self._create()
        else:
            raise RuntimeError(f"mission subscription failed: HTTP {status}")

    async def _changed(self, files: Dict[str, bytes], uids_differ: bool) -> Dict[str, bytes]:
        """Areas that are new or differ from what TAK stores. If the stored
        items cannot be read, everything is sent when the set of areas changed
        (the old behaviour)."""
        if not files:
            return {}
        stored: Optional[Dict[str, Tuple[Any, ...]]] = None
        try:
            status, body = await self.server.mission.get_mission_cot(self.s.mission_name)
            if status == 200:
                stored = parse_signatures(body)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOG.debug("Cannot read stored mission items: %s", exc)
        if stored is None:
            return dict(files) if uids_differ else {}
        return {uid: c for uid, c in files.items()
                if stored.get(uid) != cot_signature(ET.fromstring(c))}

    async def run_once(self) -> bool:
        """One round. True if a package was uploaded."""
        await self.subscribe()
        status, mission = await self.server.mission.get_mission(self.s.mission_name)
        if status == 404:
            await self._create()
            return False
        if status != 200:
            raise RuntimeError(f"cannot get mission: HTTP {status}")

        xml = await fetch(cap.FEED_URL.format(lang=self.s.lang), self.headers, timeout=30)
        areas = cap.parse(xml, self.s.lang, self.s.urgencies, self.s.event_codes)
        wanted = {a.uid for a in areas}
        present = {u.get("data") for u in mission["data"][0].get("uids", [])
                   if isinstance(u, dict)}

        for uid in sorted(present - wanted):
            LOG.info("Removing expired warning %s", uid)
            s, r = await self.server.mission.remove_mission_content(
                self.s.mission_name, uid, self.s.my_uid, self.token)
            if s != 200:
                LOG.warning("Could not remove %s: HTTP %s %s", uid, s, r)
            await asyncio.sleep(3)  # ATAK's DataSync gets confused by fast changes

        files = {a.uid: cot.area_cot(a, self.s.my_uid, self.s.mission_name, self.tz,
                                     self.s.source_line) for a in areas}
        upload = await self._changed(files, wanted != present)
        if not upload:
            LOG.info("%d warnings, no changes", len(wanted))
            return False
        package = self.build_package(
            name=self.s.mission_name, mission_name=self.s.mission_name,
            mission_server=f"{self.s.api_host}:8443:ssl", creator_uid=self.s.my_uid,
            cot_files=upload, groups=self.s.mission_groups or None)
        LOG.info("Uploading %d of %d warning areas", len(upload), len(files))
        s, r = await self.server.mission.add_mission_package(
            self.s.mission_name, self.s.my_uid, self.token, package)
        if s != 200:
            raise RuntimeError(f"package upload failed: HTTP {s} {r}")
        return True


class WarningsSource:
    """Source for run_feeder: the keep-alive contact on the CoT stream, and the
    mission sync loop in the background."""

    def __init__(self, settings: Settings, health: Optional[Health] = None) -> None:
        self.s = settings
        self.health = health

    async def events(self) -> list[bytes]:
        """The FMI contact, every round."""
        return [cot.keep_alive(self.s.my_uid, __version__)]

    async def start(self) -> None:
        """Sync loop. Every failure waits RETRY_INTERVAL; it never spins."""
        # pylint: disable=import-outside-toplevel
        from python_takserver_api import Server, build_mission_package  # type: ignore

        server = Server(self.s.api_host, self.s.cert, self.s.key)
        sync = MissionSync(self.s, server, build_mission_package)
        try:
            while True:
                try:
                    uploaded = await sync.run_once()
                    if self.health is not None:
                        self.health.ok()
                    if uploaded:
                        await asyncio.sleep(30)  # let clients settle (upstream note)
                    wait = self.s.update_interval
                except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
                    LOG.warning("Warning sync failed: %s; retrying in %.0f s",
                                exc, self.s.retry_interval)
                    if self.health is not None:
                        self.health.error()
                    wait = self.s.retry_interval
                await asyncio.sleep(wait)
        finally:
            await server.close()
