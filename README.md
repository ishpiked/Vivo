# Vivarium API

A self-hosted companion API for vivarium.su, shaped like enc-dec.app (which only covers cinejoy). It signs requests the same way the site's own frontend does, fetches stream links across all providers in the background, and presents them through two foreground servers: Aster for English dub anime, Vexa for Japanese audio with subtitles.

## What it does

- Signs vivarium.su API calls (nonce + timestamp + WASM signature), so clients never touch the site's protection directly.
- One-shot stream lookup by TMDB id (movies/series) or internal id (anime).
- Parallel sub-server race: first provider link that fits wins, with full-list fallback.
- Parsed quality tables on every result (label, height, provider, dub/hevc/hdr flags).
- HLS links only. Video bytes always stream straight from vivarium's CDNs, never through this API, so hosting it costs almost no bandwidth.

## Endpoints

Successful API responses use the enc-dec.app envelope:
`{"status": 200, "result": {...}}`. A source lookup with no usable HLS link
returns HTTP 404 and `code: "no_sources"`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/health` | Lightweight local health check; no upstream request. |
| GET | `/api/servers` | Foreground servers: Aster, Vexa |
| GET | `/api/health-vivarium` | All background providers and their status |
| GET | `/api/vivarium?id=&type=&s=&e=&server=&race=` | One-shot lookup. `type` is `movie` or `tv`; `s`/`e` are season/episode for tv. `server` is `aster` or `vexa`. `race=true` (default) returns the first fitting link; `race=false` returns the full source list. |
| GET | `/api/enc-vivarium?id=&type=&s=&e=` | Signed request kit (`path`, `headers`, `cookies`, `url`). Fetch it yourself, like `enc-cinejoy`. |
| POST | `/api/dec-vivarium` | Filter a raw `/api/e` response: `{"response": {...}, "dub": false, "provider": null, "server": null}` |
| GET | `/api/status` | Dashboard summary: uptime, request/lookup counters, caches, signing/bootstrap readiness. |
| GET | `/api/status/metrics` | Request totals by route and HTTP status, latency summary, empty lookup count. |
| GET | `/api/status/cache` | Cache sizes, entry cap, and TTLs. |
| GET | `/api/status/crypto` | Signing readiness, nonce pool size, VG expiry, and bootstrap state (no credential values). |
| GET | `/api/admin/status` | Key sources, `vg` expiry, bootstrap state |
| POST | `/api/admin/vg` | Hot-swap the `vg` cookie: `{"vg": "..."}`. No restart; currently unauthenticated, so use only in a trusted environment. |

The dashboard endpoints expose process-local counters and cache state; values
reset when the service restarts. `/api/health-vivarium` separately checks the
upstream provider health and can take as long as the upstream request.
`/api/status` is local-only and does not make an upstream request. Its
`state` is `ready` only when the signing material and WASM are present and the
known VG expiry has not passed; it does not promise that Vivarium's providers
are currently returning streams.

If no matching HLS stream with an HTTP(S) URL is available, `/api/vivarium` and
`/api/dec-vivarium` return HTTP `404` with `code: "no_sources"` and a
retry/provider health hint instead of caching or returning an empty successful
result. Empty results are not cached, including stale empty entries, so a
later request can discover newly available links.

## Credentials and renewal

The API signs Vivarium's protected `/api/e` and `/api/es` requests. There is no
tested keyless path to stream results: an unsigned `/api/n` request returned
HTTP 403, and unsigned `/api/e` returned no streams. Avoid third-party
extractors or browser automation workarounds; they do not remove the upstream
authorization requirement.

- **`VIVARIUM_VG`** is the site cookie used to obtain nonces. It has an embedded
  expiry (typically about 30 days). Check `GET /api/status/crypto` or
  `GET /api/admin/status`; these report source and expiry, never the cookie
  value.
- **`VIVARIUM_U_HEX` and `VIVARIUM_XCV`** are signing parameters, not expiring
  user tokens. Leave them empty: the service derives them from Vivarium's
  current JavaScript in the background and validates the result. Check
  `/api/status/crypto` for `crypto_source`, `bootstrap.ok`, and `bootstrap.last`.
  If the site changes its signing implementation and bootstrap fails, update
  the app and investigate the reported bootstrap error before pinning overrides.

When VG expires or `/api/status/crypto` reports nonce/bootstrap failures:

1. Open `https://vivarium.su` in your browser and complete any site challenge.
2. In browser DevTools, open **Application/Storage → Cookies → vivarium.su**
   and copy the current `vg` value. Never put the value in a URL, issue, or
   committed file.
3. **Recommended for Render:** update the `VIVARIUM_VG` environment variable
   in the Render service settings and redeploy/restart the service.
4. For a running service, `POST /api/admin/vg` also hot-swaps the value. This
   endpoint currently has no authentication: use it only in a trusted/private
   environment. Do not expose it to untrusted clients; prefer the Render
   dashboard setting for the public service.
5. Confirm `/api/status/crypto` shows a future VG expiry and a healthy nonce
   pool. Retry the requested title. A 403 or `no_sources` can also indicate a
   Vivarium/provider outage, not necessarily an expired cookie.

For local development, update `VIVARIUM_VG` in the ignored `.env` file and
restart the API. Leave the two crypto overrides blank unless diagnosing a
confirmed site-side signing change. The optional browser refresh only works
when SeleniumBase and a usable Chrome browser are installed; the standard
Render Python service does not provide Chrome, so plan on manual VG renewal
there.

Examples (Jujutsu Kaisen S3E4, internal id 95479, absolute numbering):

```
GET /api/vivarium?id=95479&type=tv&s=1&e=51&server=vexa
GET /api/vivarium?id=95479&type=tv&s=1&e=51&server=aster&race=true
```

## Player integration

The API only hands out links. Your player fetches video bytes straight from
the CDN, so send these headers on every stream and subtitle request:

```
Referer: https://vivarium.su/
User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36
```

Without the Referer the CDN answers 403. Subtitle tracks arrive as separate
`subs` URLs inside each stream object (`{url, lang, label}`); load the
English one as an external track.

mpv:

```
mpv --referrer=https://vivarium.su/ "<stream url>" --sub-file="<en sub url>"
```

Browser with hls.js: `fetch` the `/api/vivarium` URL (CORS is open), pass
`result.streams[i].url` to hls.js with `xhrSetup` adding the Referer, and add
the English sub track to a `<track>` element. Heavy local playback
(parallel segment mirror, seek cache) lives in `smooth.py` and is meant to
run on your own machine, not on the deployed API.

## Quickstart (local)

Requirements: Python 3.10+ (Python 3.11.9 is used by the Render Blueprint).
The pinned direct runtime dependencies are listed in `requirements.txt`; install
them into a virtual environment:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

1. Copy `.env.example` to `.env` and set `VIVARIUM_VG` from your browser
   (Vivarium site → DevTools → Application/Storage → Cookies). Leave
   `VIVARIUM_U_HEX` and `VIVARIUM_XCV` empty; they are derived automatically.
2. Run: `uvicorn api:app --host 127.0.0.1 --port 8000`
3. Check: `http://127.0.0.1:8000/api/status`

No keys live in the code. `.env` and `.vivcrypto.json` are git-ignored.
`U_HEX` / `X_CV` are re-derived from the site's own JavaScript in the
background and hot-swap in once validated. VG browser auto-refresh is optional
and only works when SeleniumBase and a usable Chrome browser are installed;
otherwise renew VG manually as described below.

The Render service uses the same direct dependencies in
`requirements.txt` and has `PYTHON_VERSION=3.11.9` in `render.yaml`. When
updating dependency pins, update direct runtime packages there rather than
pinning implementation-only transitive packages such as `pydantic-core` or
`typing-extensions` independently.

## Deploy (Render)

1. Push this folder to GitHub (take `api.py`, `vivcrypto.py`, `requirements.txt`, `render.yaml`, `.gitignore`, `LICENSE`; `smooth.py` / `vivarium.py` are local player scripts and optional).
2. Render, New, Web Service, connect the repo (set Root Directory to this folder if it is not the repo root).
3. Build: `pip install -r requirements.txt`. Start: `uvicorn api:app --host 0.0.0.0 --port $PORT`. Or deploy the included `render.yaml` as a Blueprint.
4. Env vars: `PYTHON_VERSION=3.11.9`, `VIVARIUM_VG=<paste the cookie>`.
   If creating the service manually instead of from `render.yaml`, set these
   in **Settings → Environment**; the Blueprint file is not applied.
5. Verify `/health`, `/api/status`, and `/api/status/crypto`.

`requirements.txt` pins the direct runtime dependencies: FastAPI/Pydantic for
the API, Uvicorn for serving, Requests for the crypto bootstrap, HTTPX with
HTTP/2 for pooled async lookups, Wasmtime for request signing, and
Cryptography for bootstrap decryption. Transitive packages are intentionally
not separately pinned.

Notes: Render disks are ephemeral, so `VIVARIUM_VG` in the dashboard is the
source of truth. The native Python runtime has no Chrome, so browser
auto-refresh stays dormant there. If the cookie expires, update `VIVARIUM_VG`
in Render settings and redeploy/restart; avoid the unauthenticated
`POST /api/admin/vg` on a public service. Responses cache for 30s, which keeps
upstream load (and latency on repeats) low without holding HLS links as long.
Lookup requests reuse a pooled async HTTP client and negotiate HTTP/2 when the
upstream supports it. Set the `vivarium.performance` logger to `INFO` to record
per-stage `scrape_timing_ms` measurements (upstream request, parsing, source
extraction, search-to-source, and response generation) plus end-to-end
`scrape_response_ms`. `race=true` (the default) consumes the upstream event
stream and returns the first matching HLS source; set `race=false` when the
complete quality/source list is required. The upstream already supplies direct HLS URLs, so the API
returns a fitting URL immediately without a HEAD/playlist probe that would add
another network round trip.

### Best-effort keep-alive on Render Free

Render Free web services spin down after 15 minutes without inbound traffic.
An external HTTP monitor can request `/health` every 5 minutes to reduce idle
spin-downs; the endpoint performs no upstream work. Configure a free HTTP
monitor in [UptimeRobot](https://uptimerobot.com/) with:

1. Monitor type: **HTTP(s)**.
2. URL: `https://kitsu-backend-2mbi.onrender.com/health`.
3. Monitoring interval: **5 minutes** (available on UptimeRobot's Free plan).
4. Expected status: HTTP `200`.

The repository also includes a GitHub Actions scheduled probe every 5 minutes
as a separate best-effort fallback:

1. Push the workflow to the repository's default branch.
2. Confirm **Actions** are enabled. The workflow targets
   `https://kitsu-backend-2mbi.onrender.com/health`; you can run
   **Keep Render service warm → Run workflow** to check it manually.

Every `/health` request is logged with a UTC timestamp, path, HTTP status,
latency, and the incoming `CF-Ray` identifier when present. `/robots.txt` is
not used: Render may answer it itself while the service is asleep. The HTTP
request to `/health` uses Render's normal wake-up behavior; cold starts can
take about a minute, so the scheduled probe allows retries.

This is best effort, not an uptime guarantee. Third-party monitors and GitHub
scheduled workflows can be delayed or disabled, and Render can restart Free
services at any time. Render documents no persistent-execution option for Free
Web Services: they spin down after 15 minutes without inbound traffic, and a
subsequent request wakes them. Free instances also share 750 instance hours
per workspace per calendar month; if the workspace exhausts that amount,
Render suspends its Free Web Services until the next month. A monitor cannot
override those platform limits. Staying continuously available is only
practical while the workspace remains within its included hours and Render
permits the instance to run; guaranteed persistent execution requires a paid
instance or another host with an always-on free tier.

## Files

- `api.py` - the FastAPI service described above.
- `vivcrypto.py` - shared signing core: WASM signer, nonce pool, key bootstrap + refresh.
- `requirements.txt`, `render.yaml` - Render deploy surface.
- `.github/workflows/render-keepalive.yml` - scheduled health check for Render Free.
- `player/` (local only, not pushed) - smooth player and lookup script. Heavy
  playback runs on your own machine; the API only hands out links.

## License

MIT (c) spike. See `LICENSE`.
