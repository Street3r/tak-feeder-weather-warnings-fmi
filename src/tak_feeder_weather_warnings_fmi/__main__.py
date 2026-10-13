# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright Stefan Gofferje (tak-feeder-weather-warnings-fmi)
# Copyright (C) 2026 Street3r (changes, see README)
"""Run the feeder: python -m tak_feeder_weather_warnings_fmi"""

from __future__ import annotations

import asyncio
import logging

from . import __version__
from .common import Health, TakSettings, run_feeder, setup_logging
from .feeder import Settings, WarningsSource

LOG = logging.getLogger("tak_feeder_weather_warnings_fmi")


async def main() -> None:
    """Read settings and run."""
    tak = TakSettings.from_env()
    setup_logging(tak.debug)
    settings = Settings.from_env()
    LOG.info("tak-feeder-weather-warnings-fmi %s -> mission %s on %s (groups %s)",
             __version__, settings.mission_name, settings.api_host,
             ",".join(settings.mission_groups) or "default")
    await run_feeder(tak, WarningsSource(settings, Health()), 30)


def run() -> None:
    """Console script entry point."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
