# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Street3r
#
# Shared helpers for the Street3r forks of Stefan Gofferje's TAK feeders.
# The same file is kept in every fork; change it everywhere at once.
"""Settings, health, HTTP, CoT and the TAK send loop shared by the feeders."""

from __future__ import annotations

import asyncio
import gzip
import logging
import os
import time
import urllib.request
import xml.etree.ElementTree as ET
from configparser import ConfigParser, SectionProxy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence

import pytak

LOG = logging.getLogger("tak_feeder")

# Old variable names still accepted, so existing .env files keep working.
LEGACY_ALIASES: dict[str, tuple[str, ...]] = {
    "COT_URL": ("REMOTE_SERVER_URL",),
    "PYTAK_TLS_CLIENT_CERT": ("CLIENT_CERT", "REMOTE_SSL_USER_CERTIFICATE"),
    "PYTAK_TLS_CLIENT_KEY": ("CLIENT_KEY", "REMOTE_SSL_USER_KEY"),
}


# --------------------------------------------------------------------- env
def env(
    name: str,
    default: str = "",
    *aliases: str,
    environ: Optional[Mapping[str, str]] = None,
) -> str:
    """First non-empty value of NAME, then of its aliases, else the default."""
    source = os.environ if environ is None else environ
    for key in (name, *aliases, *LEGACY_ALIASES.get(name, ())):
        value = source.get(key)
        if value is not None and value.strip() != "":
            return value.strip()
    return default


def env_bool(name: str, default: bool = False, *aliases: str,
             environ: Optional[Mapping[str, str]] = None) -> bool:
    """Boolean env value: 1/true/yes/on are true."""
    value = env(name, "", *aliases, environ=environ)
    if value == "":
        return default
    return value.lower() in ("1", "true", "yes", "on")


def env_float(name: str, default: float, *aliases: str,
              environ: Optional[Mapping[str, str]] = None) -> float:
    """Float env value; invalid values fall back to the default with a warning."""
    value = env(name, "", *aliases, environ=environ)
    if value == "":
        return default
    try:
        return float(value)
    except ValueError:
        LOG.warning("%s=%r is not a number, using %s", name, value, default)
        return default


def env_int(name: str, default: int, *aliases: str,
            environ: Optional[Mapping[str, str]] = None) -> int:
    """Integer env value."""
    return int(env_float(name, float(default), *aliases, environ=environ))


def env_list(name: str, default: str = "", *aliases: str,
             environ: Optional[Mapping[str, str]] = None) -> list[str]:
    """Comma-separated env value as a list without empty items."""
    return [p.strip() for p in env(name, default, *aliases, environ=environ).split(",")
            if p.strip()]


def setup_logging(debug: bool) -> None:
    """Plain timestamped logging to stdout."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        force=True,
    )


# --------------------------------------------------------------------- TAK
@dataclass
class TakSettings:
    """TAK connection settings, passed to pytak."""

    cot_url: str = "udp://239.2.3.1:6969"
    tls_client_cert: str = ""
    tls_client_key: str = ""
    tls_client_cafile: str = ""
    tls_dont_verify: bool = False
    tak_proto: str = ""
    debug: bool = False

    @classmethod
    def from_env(cls, environ: Optional[Mapping[str, str]] = None) -> "TakSettings":
        """Read the TAK settings (old variable names included)."""
        cert = env("PYTAK_TLS_CLIENT_CERT", environ=environ)
        key = env("PYTAK_TLS_CLIENT_KEY", environ=environ)
        return cls(
            cot_url=env("COT_URL", cls.cot_url, environ=environ),
            tls_client_cert=os.path.abspath(cert) if cert else "",
            tls_client_key=os.path.abspath(key) if key else "",
            tls_client_cafile=env("PYTAK_TLS_CLIENT_CAFILE", environ=environ),
            tls_dont_verify=env_bool("PYTAK_TLS_DONT_VERIFY", False, environ=environ),
            tak_proto=env("TAK_PROTO", environ=environ),
            debug=env_bool("DEBUG", False, environ=environ),
        )

    def pytak_config(self) -> SectionProxy:
        """pytak configuration. Boolean keys are only set when true."""
        cfg = {"COT_URL": self.cot_url}
        if self.tls_client_cert:
            cfg["PYTAK_TLS_CLIENT_CERT"] = self.tls_client_cert
        if self.tls_client_key:
            cfg["PYTAK_TLS_CLIENT_KEY"] = self.tls_client_key
        if self.tls_client_cafile:
            cfg["PYTAK_TLS_CLIENT_CAFILE"] = self.tls_client_cafile
        if self.tls_dont_verify:
            cfg["PYTAK_TLS_DONT_VERIFY"] = "1"
        if self.tak_proto:
            cfg["TAK_PROTO"] = self.tak_proto
        if self.debug:
            cfg["DEBUG"] = "1"
        parser = ConfigParser(interpolation=None)
        parser["feeder"] = cfg
        return parser["feeder"]


# ------------------------------------------------------------------ health
class Health:
    """Docker health flag. The file exists only while the feeder is unhealthy.

    Unhealthy after HEALTH_MAX_ERRORS failed rounds in a row, or when no data
    has arrived for HEALTH_MAX_SILENCE seconds (0 disables that check).
    """

    def __init__(self, path: str = "", max_errors: int = 0, max_silence: float = -1) -> None:
        self.path = path or env("HEALTH_FILE", "/tmp/tak-feeder-unhealthy")
        self.max_errors = max_errors or env_int("HEALTH_MAX_ERRORS", 3)
        self.max_silence = (
            max_silence if max_silence >= 0 else env_float("HEALTH_MAX_SILENCE", 0.0)
        )
        self.errors = 0
        self.last_data = time.monotonic()
        self._set(True)

    def _set(self, healthy: bool) -> None:
        try:
            if healthy:
                if os.path.exists(self.path):
                    os.unlink(self.path)
            else:
                with open(self.path, "w", encoding="utf-8") as fh:
                    fh.write("unhealthy\n")
        except OSError as exc:
            LOG.debug("Health file %s: %s", self.path, exc)

    def ok(self) -> None:
        """A successful round or new data."""
        self.errors = 0
        self.last_data = time.monotonic()
        self._set(True)

    def error(self) -> None:
        """A failed round."""
        self.errors += 1
        if self.errors >= self.max_errors:
            self._set(False)

    def check_silence(self) -> None:
        """Mark unhealthy when data has stopped arriving."""
        if self.max_silence > 0 and time.monotonic() - self.last_data > self.max_silence:
            self._set(False)


# -------------------------------------------------------------------- HTTP
def http_get(url: str, headers: Optional[Mapping[str, str]] = None, timeout: float = 30.0) -> bytes:
    """GET a URL. Asks for gzip and decompresses it."""
    hdrs = {"Accept-Encoding": "gzip", **(headers or {})}
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        body: bytes = resp.read()
        if resp.headers.get("Content-Encoding", "").lower() == "gzip":
            body = gzip.decompress(body)
    return body


async def fetch(url: str, headers: Optional[Mapping[str, str]] = None, timeout: float = 30.0) -> bytes:
    """http_get in a worker thread so the event loop keeps running."""
    return await asyncio.to_thread(http_get, url, headers, timeout)


def digitraffic_headers(user: str, app: str, version: str) -> dict[str, str]:
    """Headers Fintraffic asks Digitraffic clients to send (no personal data)."""
    return {"Digitraffic-User": user, "User-Agent": f"{app}/{version}"}


# --------------------------------------------------------------------- CoT
def cot_time(dt: Optional[datetime] = None) -> str:
    """CoT timestamp, UTC with milliseconds."""
    value = (dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def remarks_text(lines: Iterable[Optional[str]], source: str = "", tags: Sequence[str] = ()) -> str:
    """Remarks: info lines, then the data source line, then hashtags."""
    out = [line for line in lines if line]
    if source:
        out.append(source)
    tag_line = " ".join(t if t.startswith("#") else f"#{t}" for t in tags if t)
    if tag_line:
        out.append(tag_line)
    return "\n".join(out)


def cot_event(
    uid: str,
    cot_type: str,
    lat: float,
    lon: float,
    callsign: str,
    remarks: str = "",
    stale: float = 120,
    how: str = "m-g",
    hae: float = 0.0,
    ce: float = 10.0,
    le: float = 10.0,
    course: Optional[float] = None,
    speed: Optional[float] = None,
    icon: str = "",
    color: str = "",
    now: Optional[datetime] = None,
    stale_at: Optional[datetime] = None,
    extra: Iterable[ET.Element] = (),
) -> bytes:
    """One CoT event. Speed in m/s, course in degrees."""
    t = now or datetime.now(timezone.utc)
    root = ET.Element("event", {
        "version": "2.0",
        "type": cot_type,
        "uid": uid,
        "how": how,
        "time": cot_time(t),
        "start": cot_time(t),
        "stale": cot_time(stale_at or t + timedelta(seconds=stale)),
    })
    ET.SubElement(root, "point", {
        "lat": f"{lat:.6f}", "lon": f"{lon:.6f}",
        "hae": f"{hae:.1f}", "ce": f"{ce:.1f}", "le": f"{le:.1f}",
    })
    detail = ET.SubElement(root, "detail")
    ET.SubElement(detail, "contact", {"callsign": callsign})
    if course is not None or speed is not None:
        ET.SubElement(detail, "track", {
            "course": f"{(course or 0.0) % 360:.1f}", "speed": f"{max(speed or 0.0, 0.0):.2f}",
        })
    if icon:
        ET.SubElement(detail, "usericon", {"iconsetpath": icon})
    if color:
        ET.SubElement(detail, "color", {"argb": color})
    for element in extra:
        detail.append(element)
    if remarks:
        ET.SubElement(detail, "remarks").text = remarks
    return ET.tostring(root)


def as_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    """Float from JSON, or the default for null/invalid values."""
    if value is None or isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return default if result != result else result  # NaN -> default


# ----------------------------------------------------------- send to TAK
class Source(Protocol):
    """A data source: start() runs in the background, events() is polled."""

    async def start(self) -> None:
        """Start background work (may return at once)."""

    async def events(self) -> list[bytes]:
        """CoT events to send this round."""


class Sender(pytak.QueueWorker):  # type: ignore[misc]
    """Polls the source every interval seconds and queues its CoT events."""

    def __init__(self, queue: asyncio.Queue[bytes], config: SectionProxy,
                 source: Source, interval: float) -> None:
        super().__init__(queue, config)
        self.source = source
        self.interval = interval

    async def handle_data(self, data: bytes) -> None:
        """Unused: events come from the source."""

    async def run(self, number_of_iterations: int = -1) -> None:
        """Send loop. Waits for room in the queue: pytak's put_queue drops the
        oldest item when the queue (MAX_OUT_QUEUE, 100) is full, which lost
        all but the last 100 events of a larger round."""
        while number_of_iterations != 0:
            number_of_iterations -= 1
            for event in await self.source.events():
                await self.queue.put(event)
            await asyncio.sleep(self.interval)


class RxDrain(pytak.QueueWorker):  # type: ignore[misc]
    """Empties the RX queue; feeders do not use incoming CoT."""

    async def handle_data(self, data: bytes) -> None:
        """Ignore."""

    async def run(self, number_of_iterations: int = -1) -> None:
        """Drain forever."""
        while number_of_iterations != 0:
            number_of_iterations -= 1
            await self.queue.get()


def _log_task_failure(task: "asyncio.Task[None]") -> None:
    if not task.cancelled() and task.exception() is not None:
        LOG.error("Source stopped: %r", task.exception())


async def run_feeder(tak: TakSettings, source: Source, interval: float) -> None:
    """Start the source, then send to TAK. pytak reconnects after TAK outages
    without restarting the source, so cached data survives."""
    config = tak.pytak_config()
    background = asyncio.create_task(source.start())
    background.add_done_callback(_log_task_failure)

    async def run_once() -> None:
        clitool = pytak.CLITool(config)
        await clitool.setup()
        clitool.add_tasks({Sender(clitool.tx_queue, config, source, interval),
                           RxDrain(clitool.rx_queue, config)})
        await clitool.run()

    try:
        await pytak.supervise_with_reconnect(config, run_once)
    finally:
        background.cancel()


# -------------------------------------------------------------------- MQTT
def start_mqtt(
    name: str,
    url: str,
    topics: Sequence[str],
    on_message: Any,
    client_id: str,
    username: str = "",
    password: str = "",
) -> Any:
    """Connect in the background (paho keeps reconnecting with back-off) and
    subscribe to the topics on every (re)connect. on_message(topic, payload)."""
    from urllib.parse import urlparse  # pylint: disable=import-outside-toplevel

    import paho.mqtt.client as mqtt  # pylint: disable=import-outside-toplevel

    parsed = urlparse(url if "://" in url else f"mqtt://{url}")
    scheme = parsed.scheme.lower()
    websockets = scheme in ("ws", "wss")
    tls = scheme in ("wss", "mqtts", "ssl")
    port = parsed.port or (443 if scheme == "wss" else 80 if scheme == "ws"
                           else 8883 if tls else 1883)
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        transport="websockets" if websockets else "tcp",
    )
    if websockets:
        client.ws_set_options(path=parsed.path or "/mqtt")
    if tls:
        client.tls_set()
    if username:
        client.username_pw_set(username, password or None)
    client.reconnect_delay_set(min_delay=5, max_delay=300)

    def _on_connect(c: Any, _u: Any, _f: Any, reason: Any, _p: Any = None) -> None:
        if getattr(reason, "is_failure", False):
            LOG.warning("%s: MQTT connect refused: %s", name, reason)
            return
        LOG.info("%s: connected to %s", name, parsed.hostname)
        for topic in topics:
            c.subscribe(topic)

    def _on_disconnect(_c: Any, _u: Any, _f: Any, reason: Any, _p: Any = None) -> None:
        LOG.warning("%s: MQTT disconnected (%s), reconnecting", name, reason)

    def _on_connect_fail(_c: Any, _u: Any) -> None:
        LOG.warning("%s: cannot connect to %s:%s, retrying", name, parsed.hostname, port)

    def _on_message(_c: Any, _u: Any, msg: Any) -> None:
        try:
            on_message(msg.topic, msg.payload)
        except Exception as exc:  # noqa: BLE001  pylint: disable=broad-exception-caught
            LOG.debug("%s: bad message on %s: %s", name, msg.topic, exc)

    client.on_connect = _on_connect
    client.on_disconnect = _on_disconnect
    client.on_connect_fail = _on_connect_fail
    client.on_message = _on_message
    client.connect_async(parsed.hostname or "localhost", port, keepalive=60)
    client.loop_start()
    return client
