# tak-feeder-weather-warnings-fmi

> [!WARNING]
> **Work in progress. Not recommended for use.** This fork is being improved
> and has not been tested against a live TAK server yet. Things may change or
> break without notice.

Gets the weather warnings of the Finnish Meteorological Institute (FMI) and
keeps a TAK server mission in sync with them: each warning area is a polygon
in the mission, and expired warnings are removed.

Fork of [sgofferj/tak-feeder-weather-warnings-fmi](https://github.com/sgofferj/tak-feeder-weather-warnings-fmi)
by Stefan Gofferje.

> [!NOTE]
> The mission is delivered with the Mission API, so clients need the DataSync
> plugin. Depending on the filters a mission can hold hundreds of polygons,
> which is heavy for older devices and causes traffic. Choose
> `UPDATE_INTERVAL` and the filters with care (upstream note).

## What it sends

| Field | Value |
|---|---|
| UID | hash of the warning, area and publication time (as upstream) |
| Callsign | `FMI <event> <area>`, e.g. `FMI Wind warning for land areas Uusimaa` |
| Type | `u-d-f` polygon, filled yellow / orange / red by warning level |
| Start / stale | onset and expiry of the warning |
| Remarks | headline, description, level, validity (local time), source, `#weather #warning #<event code>` |

The feeder also keeps an `FMI` contact on the CoT stream; it is the creator
of the mission items (as upstream).

### Mission groups

`MISSION_GROUP` takes one or more TAK groups, comma-separated. A new mission
is created for all of them, so for example `WEATHER` lets members of the
`WEATHER` group subscribe. The groups apply when the mission is created; for
an existing mission change them in the TAK admin UI or delete the mission.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `COT_URL` | `udp://239.2.3.1:6969` | TAK CoT stream for the FMI contact |
| `PYTAK_TLS_CLIENT_CERT` / `PYTAK_TLS_CLIENT_KEY` | | Client certificate and unencrypted key (PEM), also used for the Mission API. Old names `CLIENT_CERT` / `CLIENT_KEY` work |
| `PYTAK_TLS_DONT_VERIFY` | `0` | `1` = do not verify the server certificate |
| `API_HOST` | | TAK server for the Mission API (port 8443) |
| `MISSION_NAME` | `Weatherwarnings` | Mission name |
| `MISSION_GROUP` | | Group(s) of a new mission, comma-separated |
| `MISSION_TOKEN` | | Mission token, if the feeder cannot subscribe itself |
| `MY_UID` | `fmi.0001` | UID of the FMI contact |
| `FMI_LANG` | `en-GB` | Feed language: `en-GB`, `fi-FI`, `sv-FI` |
| `FILTER_URGENCY` | `Expected,Immediate` | Urgencies to include (`Immediate`, `Expected`, `Future`) |
| `FILTER_EVENTCODE` | all except `uvNote` | Warning types (see below) |
| `UPDATE_INTERVAL` | `3600` | Seconds between rounds |
| `RETRY_INTERVAL` | `300` | Wait after a failed round |
| `TIMEZONE` | `Europe/Helsinki` | Time zone of the validity times |
| `HEALTH_MAX_ERRORS` | `3` | Failed rounds in a row before the container is unhealthy |
| `DEBUG` | `0` | Debug logging |

Warning types of the [FMI CAP profile](https://alerts.fmi.fi/cap/profile/v1.0.0/fmi-cap-profile-v1.0.0.md):
`coldWeather`, `forestFireWeather`, `grassFireWeather`, `hotWeather`,
`pedestrianSafety`, `rain`, `seaIcing`, `seaThunderstorm`, `seaWaterHeight`,
`seaWaveHeight`, `seaWind`, `thunderstorm`, `trafficWeather`, `uvNote`, `wind`.

## Running

Image for amd64 and arm64: `ghcr.io/street3r/tak-feeder-weather-warnings-fmi:latest`.

```yaml
services:
  feeder-weather-warnings:
    image: ghcr.io/street3r/tak-feeder-weather-warnings-fmi:latest
    restart: unless-stopped
    env_file: .env
    volumes:
      - ./certs:/data:ro
```

The container runs as uid 10001; the key must be readable by it.

## Data

Finnish Meteorological Institute open data,
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The source line is
in every warning's remarks.

## Credits and changes

Original work © Stefan Gofferje,
[tak-feeder-weather-warnings-fmi](https://github.com/sgofferj/tak-feeder-weather-warnings-fmi),
used with his permission. Uses his
[python-takserver-api](https://github.com/sgofferj/python-takserver-api).

Changes in this fork (Street3r):

- 2026-10-13
  - **Fixed:** when the TAK API answered something else than 200/404, or
    creating the mission failed, the loop ran again at once and flooded the
    TAK server. Every failure now waits `RETRY_INTERVAL`.
  - **Fixed:** the FMI request had no timeout or error handling; a network
    error stopped the warning loop. It is now asynchronous with a timeout.
  - **Fixed:** a warning without a colour inherited the colour of the previous
    one; an area without a polygon stopped the update. Broken entries are now
    skipped.
  - All FMI warning types except UV advisories are included by default
    (traffic, pedestrian, cold, sea level, wave height, ice accretion and
    grass fire warnings were missing).
  - Callsign `FMI <event> <area>`; remarks with description, level, local
    validity time, source line and `#weather #warning #<event code>` tags.
  - `MISSION_GROUP` accepts several groups.
  - Packaged as a Python module; python-takserver-api pinned to a commit
    instead of a git submodule; health check; unprivileged container; tests
    and CI.
- 2026-10-15
  - Only new or changed warning areas are uploaded. Each round compares the
    areas with what the mission already stores (type, label, remarks,
    colours, polygon); unchanged areas are not sent again.

## License

GNU General Public License v3.0 or later, see [LICENSE](LICENSE).
