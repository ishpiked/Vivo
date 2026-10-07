# Vivarium API

A self-hosted companion API for vivarium.su, shaped like enc-dec.app (which only covers cinejoy). It signs requests the same way the site's own frontend does, fetches stream links across all providers in the background, and presents them through two foreground servers: Aster for English dub anime, Vexa for Japanese audio with subtitles.

## What it does

- Signs vivarium.su API calls (nonce + timestamp + WASM signature), so clients never touch the site's protection directly.
- One-shot stream lookup by TMDB id (movies/series) or internal id (anime).
- Parallel sub-server race: first provider link that fits wins, with full-list fallback.
- Parsed quality tables on every result (label, height, provider, dub/hevc/hdr flags).
- HLS links only. Video bytes always stream straight from vivarium's CDNs, never through this API, so hosting it costs almost no bandwidth.

## Endpoints

All responses use the enc-dec.app envelope: `{"status": 200, "result": {...}}`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/api/servers` | Foreground servers: Aster, Vexa |
| GET | `/api/health-vivarium` | All background providers and their status |
| GET | `/api/vivarium?id=&type=&s=&e=&server=&race=` | One-shot lookup. `type` is `movie` or `tv`; `s`/`e` are season/episode for tv. `server` is `aster` or `vexa`. `race=true` takes the fastest fitting link. |
| GET | `/api/enc-vivarium?id=&type=&s=&e=` | Signed request kit (`path`, `headers`, `cookies`, `url`). Fetch it yourself, like `enc-cinejoy`. |
| POST | `/api/dec-vivarium` | Filter a raw `/api/e` response: `{"response": {...}, "dub": false, "provider": null, "server": null}` |
| GET | `/api/admin/status` | Key sources, `vg` expiry, bootstrap state |
| POST | `/api/admin/vg` | Hot-swap the `vg` cookie: `{"vg": "..."}`. No restart. |

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

Requirements: Python 3.10+, `pip install -r requirements.txt`.

1. Copy `.env.example` to `.env` and paste a `vg` cookie from your browser (vivarium.su, DevTools, Application, Cookies). Leave `VIVARIUM_U_HEX` / `VIVARIUM_XCV` empty unless you have pinned values.
2. Run: `uvicorn api:app --port 8000`
3. Check: `http://127.0.0.1:8000/api/servers`

No keys live in the code. `.env` and `.vivcrypto.json` are git-ignored. `U_HEX` / `X_CV` are re-derived from the site's own JavaScript in the background and hot-swap in once validated; the `vg` cookie auto-refreshes through a real browser when one with Chrome + `seleniumbase` is available, otherwise set it via env or the admin endpoint.

## Deploy (Render)

1. Push this folder to GitHub (take `api.py`, `vivcrypto.py`, `requirements.txt`, `render.yaml`, `.gitignore`, `LICENSE`; `smooth.py` / `vivarium.py` are local player scripts and optional).
2. Render, New, Web Service, connect the repo (set Root Directory to this folder if it is not the repo root).
3. Build: `pip install -r requirements.txt`. Start: `uvicorn api:app --host 0.0.0.0 --port $PORT`. Or deploy the included `render.yaml` as a Blueprint.
4. Env vars: `PYTHON_VERSION=3.11.9`, `VIVARIUM_VG=<paste the cookie>`. Note: if you created the service by hand in the dashboard instead of from `render.yaml`, the file is ignored, so set both vars in Settings, Environment by hand. Pins in `requirements.txt` all ship Python 3.14 wheels too, so a 3.14 image also builds.
5. Verify `/api/servers` and `/api/admin/status`.

Notes: Render disks are ephemeral, so `VIVARIUM_VG` in the dashboard is the source of truth. The native Python runtime has no Chrome, so browser auto-refresh stays dormant there; if the cookie dies, `POST /api/admin/vg` with a fresh one, no redeploy. Responses cache for 120s, which keeps upstream load (and latency on repeats) low.

## Files

- `api.py` - the FastAPI service described above. This is the only file the
  Render deployment needs (plus `vivcrypto.py`).
- `vivcrypto.py` - shared signing core: WASM signer, nonce pool, key bootstrap + refresh, used by everything else.
- `smooth.py` - local smooth player: parallel HLS mirror + mpv, `--server aster|vexa`, `--quality best|720p|...`. Runs on your own machine. Heavy on bandwidth by design.
- `vivarium.py` - minimal local lookup script in the same style as the cinejoy sample.

## License

MIT. Replace `ishpiked` in `LICENSE` with your own before publishing.
