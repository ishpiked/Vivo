# Vivarium Stream API

A small HTTP API for finding Vivarium stream sources for movies and TV
episodes. The hosted instance is:

```text
https://kitsu-backend-2mbi.onrender.com
```

It signs requests to Vivarium, queries its stream providers, and returns usable
HLS source URLs. Video and subtitle bytes are not proxied through this API;
your player loads those directly from the source/CDN.

## Quick start

Use a TMDB URL or a Vivarium watch URL. Quote or URL-encode the input URL in
your client so its `?` and `&` characters remain part of the `url` parameter.

```text
GET https://kitsu-backend-2mbi.onrender.com/api/vivarium?url=https%3A%2F%2Fwww.themoviedb.org%2Ftv%2F30984%2Fseason%2F2%2Fepisode%2F3&server=vexa
```

Equivalent using the Vivarium link from the Bleach example:

```text
GET https://kitsu-backend-2mbi.onrender.com/api/vivarium?url=https%3A%2F%2Fvivarium.su%2Fs%2Fbleach-30984%3Fw%3D1%26a%3D159322%26e%3D3&server=vexa
```

The second URL identifies Bleach's `a=159322` AniList course and course-local
`e=3`. Vivarium's course data maps that to TMDB S2E16. Use `server=aster` for
English dub streams; `server=vexa` requests Japanese audio with English
subtitles. Omitting `server` searches without that server filter.

Successful responses have this shape:

```json
{
  "status": 200,
  "result": {
    "streams": [
      {
        "url": "https://stream.example/playlist.m3u8",
        "type": "hls",
        "quality": "1080p",
        "provider": "Example"
      }
    ],
    "subtitles": [],
    "qualities": []
  }
}
```

The sample URL above is illustrative; actual stream URLs and metadata depend
on Vivarium's current providers and availability.

## Searching for a show

This API does **not** currently have a title-search endpoint. Search for a
show on TMDB, open the matching show/episode, then pass its URL to this API.
Alternatively, use the included [`main.py`](./main.py) reference client: its
`search` command searches TMDB, and its `stream` command sends a selected TMDB
or Vivarium URL to this API.

TMDB title search requires a TMDB API Read Access Token. Set it as
`TMDB_API_READ_ACCESS_TOKEN` in your environment; the token is sent only to
TMDB, not to the Vivarium API. You can also skip TMDB search entirely and use
an existing TMDB/Vivarium URL or the API's ID parameters.

PowerShell:

```powershell
$env:TMDB_API_READ_ACCESS_TOKEN = "your-tmdb-read-access-token"
python main.py search "Bleach"
```

## Link parameters and episode mapping

For a Vivarium watch URL such as
`https://vivarium.su/s/bleach-30984?w=1&a=159322&e=3`:

| Part | Meaning |
| ---- | ------- |
| `/s/` | Vivarium series route (`/m/` is its movie route); this is not a season number. |
| `w=1` | Vivarium watch/player mode. It is not a season or episode number. |
| `a=159322` | AniList ID for the selected anime course/title entry. |
| `e=3` | Episode number within that AniList course when `a` is present. |
| `s=...` | Optional explicit TMDB season number in the query string. |

When the Vivarium URL contains `a` and `e` but no query-string `s`, this API
fetches Vivarium's `/api/cours` data for the series ID in the path. It finds
the matching AniList course and converts that course's episode ranges into
TMDB season/episode coordinates. This handles cours that begin partway through
a TMDB season. The `w` flag does not affect stream lookup.

For a regular TMDB TV URL, include the season and episode in its path, for
example:

```text
https://www.themoviedb.org/tv/30984/season/2/episode/3
```

A show-only TMDB URL needs `s` and `e` separately:

```text
GET /api/vivarium?url=https%3A%2F%2Fwww.themoviedb.org%2Ftv%2F30984&s=2&e=3
```

## API reference

Responses use an envelope such as `{"status": 200, "result": {...}}`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Local health check; no upstream lookup. |
| GET | `/api/servers` | Lists foreground server filters (`aster`, `vexa`). |
| GET | `/api/health-vivarium` | Checks Vivarium upstream/provider health. |
| GET | `/api/vivarium?url=...` | Looks up a movie/episode from a TMDB or Vivarium URL. |
| GET | `/api/vivarium?id=&type=&s=&e=` | Looks up using explicit Vivarium/TMDB media ID and coordinates. `type` is `movie` or `tv`; TV requires season `s` and episode `e`. |
| GET | `/api/enc-vivarium?id=&type=&s=&e=` | **Protected.** Creates a signed request kit; includes the VG cookie. |
| POST | `/api/dec-vivarium` | Filters a raw `/api/e` response. |
| GET | `/api/status` | Process uptime, lookup counters, caches, and signing state. |
| GET | `/api/status/metrics` | Request totals by route/status and latency statistics. |
| GET | `/api/status/cache` | Cache sizes and TTLs. |
| GET | `/api/status/crypto` | Signing readiness and bootstrap state (does not reveal credentials). |
| GET | `/api/admin/status` | **Protected.** Detailed key sources and refresh state. |
| POST | `/api/admin/vg` | **Protected.** Updates the Vivarium `vg` cookie. |

### `/api/vivarium` options

Use either `url` or `id`/`type`:

| Parameter | Meaning |
| --------- | ------- |
| `url` | TMDB movie/episode or Vivarium `/m/...` / `/s/...` URL. |
| `id` | Vivarium/TMDB media ID. |
| `type` | `movie` or `tv`. |
| `s`, `e` | Season and episode for TV. For an AniList Vivarium link, omit `s` to map its `a`/`e` course episode. |
| `server` | `aster` (English dub) or `vexa` (Japanese audio, English subtitles). |
| `dub` | Optional boolean English-dub filter. |
| `provider` | Optional provider-name filter. |
| `race` | Defaults to `true`: returns the first suitable source. Set `false` to fetch the full source list. |

If both `url` and `id`/`type` are supplied, the API returns a 400 error. The
URL host must be `themoviedb.org` or `vivarium.su` (including their `www`
subdomains). TV links must resolve to both a season and episode before stream
lookup.

### Existing ID-based requests

The URL interface is optional. Existing integrations can continue to call:

```text
GET https://kitsu-backend-2mbi.onrender.com/api/vivarium?id=30984&type=tv&s=2&e=3&server=vexa
GET https://kitsu-backend-2mbi.onrender.com/api/vivarium?id=635302&type=movie
```

For a complete list of sources rather than the first matching source:

```text
GET https://kitsu-backend-2mbi.onrender.com/api/vivarium?id=30984&type=tv&s=2&e=3&race=false
```

### Errors

| HTTP status | Meaning |
| ----------- | ------- |
| `400` | Invalid/missing input, unsupported URL, invalid coordinates, or unmapped AniList course. |
| `401` | Missing/incorrect admin bearer password for a protected endpoint. |
| `404` | No usable HLS stream was found (`code: "no_sources"`). |
| `502` | Vivarium/course lookup failed or returned an invalid response. |
| `503` | Protected endpoint is disabled because the admin password is not configured. |

An empty stream result is not treated as success and is not cached. A
`no_sources` response can mean that providers currently have no matching
source; check `/api/health-vivarium` and try again later.

## Using the streams in a player

The API returns HLS playlist URLs and any subtitle tracks attached to those
sources. It does not proxy media traffic. Your player must load the URLs
directly. Vivarium's CDN may require these request headers:

```text
Referer: https://vivarium.su/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36
```

Subtitle entries are supplied on each stream's `subs` field when available.
Use the English track for the Vexa/subbed experience.

## Reference Python client

[`main.py`](./main.py) is a dependency-free example that can be copied into
another Python project. It exposes reusable `search_titles`,
`get_streams_by_url`, and `get_streams` functions, and a small CLI:

```powershell
# Search TMDB for a title (set TMDB_API_READ_ACCESS_TOKEN first)
python main.py search "Bleach"

# Pass a TMDB episode or Vivarium watch URL to the stream API
python main.py stream "https://vivarium.su/s/bleach-30984?w=1&a=159322&e=3" --server vexa

# Or use explicit coordinates
python main.py stream --id 30984 --type tv --season 2 --episode 16 --server vexa
```

The client defaults to this hosted service. Override it for your own
deployment with `VIVARIUM_API_BASE_URL` or the global `--api-base-url` option
before the subcommand.

## Hosting and operations

The live service is deployed on Render at
`https://kitsu-backend-2mbi.onrender.com`. Public endpoints do not need an API
key. Admin endpoints require `VIVARIUM_ADMIN_PASSWORD` and a bearer token in
the `Authorization` header; never put the password in a URL or commit it.

The service signs Vivarium's protected `/api/e` and `/api/es` requests.
`VIVARIUM_VG` is the Vivarium cookie used for nonce acquisition and expires
periodically. The signing parameters are bootstrapped from Vivarium's current
JavaScript. The protected status endpoints report readiness and expiry without
returning credential values. For deployment/renewal, configure
`VIVARIUM_VG` and `VIVARIUM_ADMIN_PASSWORD` in the hosting provider's secret
environment settings.

Responses are cached briefly in process memory (30 seconds). Counters and
caches reset when the service restarts. The API has no search endpoint and
does not relay stream bytes, so title search and playback happen in the
calling application/player.

## Run your own instance

Requirements: Python 3.10+ (Render is configured for Python 3.11.9).

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8000
```

Set the required service secrets as environment variables before deployment.
See [`render.yaml`](./render.yaml) for the Render service configuration.

## Project files

- [`api.py`](./api.py) — FastAPI service and Vivarium link/episode mapping.
- [`vivcrypto.py`](./vivcrypto.py) — request signing, nonce handling, and crypto bootstrap.
- [`main.py`](./main.py) — portable reference client and CLI.
- [`requirements.txt`](./requirements.txt) — server dependencies.
- [`render.yaml`](./render.yaml) — Render Blueprint.
