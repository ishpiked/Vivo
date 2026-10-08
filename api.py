"""Vivarium enc-dec style API (mirrors https://enc-dec.app/api/ which only supports cinejoy).

Envelope matches enc-dec.app: {"status": 200, "result": {...}}.

One-time deployment: no keys to rotate by hand. Crypto (VG cookie, U_HEX,
X_CV) is maintained by vivcrypto.py: VG auto-refreshes through a real
browser on expiry, U_HEX/X_CV are re-derived from the site's own JS in the
background. See GET /api/admin/status.

Endpoints:
  GET  /api/health-vivarium            -> providers (like wing.st/servers)
  GET  /api/servers                    -> FOREGROUND servers: Aster (english dub
                                          anime) + Vexa (japanese audio anime
                                          with English subtitles).
                                          All real providers still run in the
                                          background; only these two are shown.
  GET  /api/enc-vivarium?id=&type=&s=&e=
       -> {"path","headers","cookies"} signed request kit (like enc-cinejoy data+state).
          Requires the admin bearer password because the kit includes the VG cookie.
          Client then GETs https://vivarium.su<path> themselves.
  POST /api/dec-vivarium  {"response": <raw /api/e JSON>, "dub": bool, "provider": str|None, "server": "aster"|"vexa"|None}
       -> {"streams":[...],"subtitles":[...],"qualities":[...]} filtered (like dec-cinejoy).
  GET  /api/vivarium?url=... or ?id=&type=&s=&e=&dub=&provider=&server=&race=
       -> one-shot convenience: race=true (default) returns the first matching
          HLS source from the parallel provider event stream. race=false fetches
          the complete list when all qualities/sources are required.
  GET  /api/admin/status               -> key sources, vg expiry, bootstrap state (protected)
  POST /api/admin/vg {"vg": "..."}     -> hot-swap VG cookie (protected; no restart)

Run: uvicorn api:app --port 8000
Requires: pip install fastapi uvicorn wasmtime requests
Optional for VG auto-refresh: pip install seleniumbase (needs Chrome)
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional

import vivcrypto as vc
from vivcrypto import VIV, HEADERS

# ============================================================================
# Legacy curated anime metadata. Live link resolution uses Vivarium's
# /api/cours mapping instead of assuming this table covers every title.
# ============================================================================

VIVARIUM_ID_MAP = {
    # Confirmed 1:1 mappings from research
    30984: {
        "tmdb_id": 30984,
        "title": "Bleach",
        "type": "tv",
        "title_romaji": "Bleach",
        "seasons_tmdb": 3,  # Season 1: 366 eps, Season 2: 50 eps (TYBW), Season 3:?
        "description": "English dub anime with long-running series",
    },
    635302: {
        "tmdb_id": 635302,
        "title": "Demon Slayer Movie",
        "type": "movie",
        "title_romaji": "Kimetsu no Yaiba",
        "description": "Demon Slayer movie (Mugen Train arc)",
    },
    37854: {
        "tmdb_id": 37854,
        "title": "One Piece",
        "type": "tv",
        "title_romaji": "One Piece",
        "seasons_tmdb": 23,  # 23+ seasons, highly fragmented
        "description": "Longest-running anime with 23+ TMDB seasons",
    },
    61663: {
        "tmdb_id": 61663,
        "title": "Your Lie in April",
        "type": "tv",
        "title_romaji": "Yahari",
        "seasons_tmdb": 1,  # Simple: 1 main season + 1 special (Season 0)
        "description": "Romance drama anime, 22 episodes",
    },
    46260: {
        "tmdb_id": 46260,
        "title": "Naruto",
        "type": "tv",
        "title_romaji": "Naruto",
        "seasons_tmdb": 4,  # TMDB has 4 seasons; TVDB has 5 with different splits
        "description": "Original Naruto series, 220 episodes + specials",
    },
}


# ============================================================================
# AniHub Integration: Static mappings + Providers (aniwaves, anikoto, 2dhive)
# ============================================================================
_ANIHUB_MAPPING: dict = {}
_ANIHUB_CACHE: dict = {}
_ANIHUB_CACHE_TTL = 1800  # 30 min
_ANIHUB_CACHE_LOCK = threading.Lock()


def _load_anihub_mapping():
    """Load optional AniList title/MAL metadata for AniHub searches."""
    global _ANIHUB_MAPPING
    mapping_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "anilist_tmdb_mapping.json")
    if not os.path.isfile(mapping_path):
        _ANIHUB_MAPPING = {}
        return
    try:
        with open(mapping_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            _ANIHUB_MAPPING = {entry["anilist_id"]: entry for entry in data.get("entries", [])}
        print(f"Loaded {len(_ANIHUB_MAPPING)} AniHub mappings")
    except Exception as e:
        logging.getLogger("vivarium.anihub").warning(
            "Optional AniHub metadata could not be loaded from %s: %s",
            mapping_path, e)
        _ANIHUB_MAPPING = {}


def _get_anihub_mapping(anilist_id: int) -> dict | None:
    return _ANIHUB_MAPPING.get(anilist_id)


def _normalize_title(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", title.lower())


def _cache_anihub(key: str, data: dict) -> None:
    with _ANIHUB_CACHE_LOCK:
        _ANIHUB_CACHE[key] = {"data": data, "expires": time.time() + _ANIHUB_CACHE_TTL}


def _get_cached_anihub(key: str) -> dict | None:
    with _ANIHUB_CACHE_LOCK:
        entry = _ANIHUB_CACHE.get(key)
        if entry and time.time() < entry["expires"]:
            return entry["data"]
        if entry:
            del _ANIHUB_CACHE[key]
    return None


# Load mappings at startup
_load_anihub_mapping()


# ============================================================================
# AniHub Providers: aniwaves, anikoto, 2dhive
# ============================================================================
ANIHUB_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
ANIWAVES_BASE = "https://aniwaves.ru"
ANIKOTO_BASE = "https://anikototv.to"
ANIKOTO_MAPPER = "https://mapper.nekostream.site/api/mal"
ANIZIP_API_URL = "https://api.ani.zip/mappings"
TWODHIVE_BASE = "https://2dhive.com"

_anihub_session = None


def _get_anihub_session():
    global _anihub_session
    if _anihub_session is None:
        _anihub_session = requests.Session()
        _anihub_session.headers.update({"User-Agent": ANIHUB_UA})
    return _anihub_session


def anihub_fetch_json(url: str, headers: dict = None, cache_ttl: int = 1800) -> dict:
    key = f"anihub:{hashlib.md5(url.encode()).hexdigest()}"
    cached = _get_cached_anihub(key)
    if cached:
        return cached
    try:
        h = {"User-Agent": ANIHUB_UA, "Accept": "application/json"}
        if headers:
            h.update(headers)
        resp = _get_anihub_session().get(url, headers=h, timeout=8)
        if resp.status_code == 200:
            data = resp.json()
            _cache_anihub(key, data)
            return data
    except Exception:
        pass
    return None


def anihub_fetch_text(url: str, headers: dict = None, cache_ttl: int = 1800) -> str:
    key = f"anihub:text:{hashlib.md5(url.encode()).hexdigest()}"
    cached = _get_cached_anihub(key)
    if cached:
        return cached
    try:
        h = {"User-Agent": ANIHUB_UA, "Accept": "text/html,*/*"}
        if headers:
            h.update(headers)
        resp = _get_anihub_session().get(url, headers=h, timeout=8)
        if resp.status_code == 200:
            _cache_anihub(key, {"body": resp.text})
            return resp.text
    except Exception:
        pass
    return ""


# --- AniWaves Provider ---
def aw_search(query: str) -> list:
    try:
        html = anihub_fetch_text(f"{ANIWAVES_BASE}/filter?keyword={urlencode({'keyword': query}).split('=')[1]}")
        results = []
        for m in re.finditer(r'<a\b([^>]*)>([\s\S]*?)</a>', html, re.IGNORECASE):
            tag = m.group(1)
            if 'class="name d-title"' not in tag:
                continue
            href_m = re.search(r'href="(/watch/[^"]+)"', tag)
            if not href_m:
                continue
            slug = href_m.group(1).split("/")[-1]
            id_m = re.search(r"-(\d+)$", slug)
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            if id_m:
                results.append({"slug": slug, "site_id": int(id_m.group(1)), "title": title})
        return results
    except Exception:
        return []


def aw_resolve(anilist_id: int, media: dict = None) -> dict | None:
    titles = []
    if media:
        t = media.get("title", {})
        titles = [t.get("english"), t.get("romaji"), t.get("native")]
        titles = [x for x in titles if x]
    mapping = _get_anihub_mapping(anilist_id)
    if mapping:
        anizip = mapping.get("anizip_raw", {})
        az_t = anizip.get("titles", {}) if isinstance(anizip, dict) else {}
        titles.extend([az_t.get("en"), az_t.get("x-jat"), az_t.get("ja")])
    titles = [t for t in titles if t]
    
    best = None
    best_score = 0
    for q in titles[:4]:
        for c in aw_search(q):
            score = len(set(q.lower().split()) & set(c["title"].lower().split()))
            if score > best_score:
                best_score = score
                best = c
    if best and best_score >= 2:
        return best
    return None


def aw_get_episodes(anilist_id: int, site_id: int, slug: str) -> list:
    try:
        r = anihub_fetch_json(f"{ANIWAVES_BASE}/ajax/episode/list/{site_id}?vrf=")
        eps = []
        html = (r or {}).get("result", "")
        for m in re.finditer(r'<a\b([^>]*)>([\s\S]*?)</a>', html, re.IGNORECASE):
            tag = m.group(1)
            num_m = re.search(r'data-num="(\d+)"', tag)
            if not num_m:
                continue
            num = int(num_m.group(1))
            ep_slug = re.search(r'data-slug="([^"]+)"', tag)
            ids = re.search(r'data-ids="([^"]+)"', tag)
            sub = 'data-sub="1"' in tag
            dub = 'data-dub="1"' in tag
            title = re.sub(r"<[^>]+>", "", m.group(2)).strip()
            eps.append({"number": num, "source_number": ep_slug.group(1) if ep_slug else str(num),
                        "ids": ids.group(1) if ids else "", "title": title,
                        "has_sub": sub, "has_dub": dub})
        eps.sort(key=lambda x: x["number"])
        return eps
    except Exception:
        return []


def aw_get_servers(site_id: int, source_number: str, audio: str) -> list:
    try:
        r = anihub_fetch_json(f"{ANIWAVES_BASE}/ajax/server/list?servers={site_id}&eps={urlencode({'eps': source_number}).split('=')[1]}")
        html = (r or {}).get("result", "")

        class ServerListParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.audio_stack = []
                self.servers = []
                self.current_server = None

            def handle_starttag(self, tag, attrs):
                attributes = dict(attrs)
                if tag == "div":
                    self.audio_stack.append(
                        attributes.get("data-type")
                        or (self.audio_stack[-1] if self.audio_stack else None))
                elif tag == "li" and self.audio_stack and self.audio_stack[-1] == audio:
                    self.current_server = {
                        "attributes": attributes, "name": []}

            def handle_data(self, data):
                if self.current_server is not None:
                    self.current_server["name"].append(data)

            def handle_endtag(self, tag):
                if tag == "li" and self.current_server is not None:
                    attributes = self.current_server["attributes"]
                    link_id = attributes.get("data-link-id")
                    if link_id:
                        self.servers.append({
                            "link_id": link_id,
                            "server_id": attributes.get("data-sv-id"),
                            "server_name": "".join(
                                self.current_server["name"]).strip() or "AniWaves",
                            "audio": audio,
                        })
                    self.current_server = None
                elif tag == "div" and self.audio_stack:
                    self.audio_stack.pop()

        parser = ServerListParser()
        parser.feed(html)
        return parser.servers
    except Exception:
        return []


def aw_get_sources(link_id: str) -> dict:
    try:
        return anihub_fetch_json(f"{ANIWAVES_BASE}/ajax/sources?id={urlencode({'id': link_id}).split('=')[1]}&asi=0&autoPlay=0")
    except Exception:
        return {}


# --- Anikoto Provider ---
def ak_search(query: str) -> list:
    try:
        html = anihub_fetch_text(f"{ANIKOTO_BASE}/filter?keyword={urlencode({'keyword': query}).split('=')[1]}")
        results = []
        for m in re.finditer(r'href="https://anikototv\.to/watch/([^"/]+)(?:/ep-\d+)?"[^>]*data-jp="([^"]*)"[^>]*>([\s\S]*?)</a>', html):
            slug, jp, name = m.groups()
            results.append({"slug": slug, "jp": jp, "name": re.sub(r"<[^>]+>", "", name).strip()})
        return results
    except Exception:
        return []


def ak_resolve(anilist_id: int, media: dict = None) -> dict | None:
    titles = []
    if media:
        t = media.get("title", {})
        titles = [t.get("english"), t.get("romaji"), t.get("native")]
        titles = [x for x in titles if x]
    mapping = _get_anihub_mapping(anilist_id)
    if mapping:
        mal_id = mapping.get("mal_id")
        if mal_id:
            titles.insert(0, str(mal_id))
    
    for q in titles[:4]:
        for c in ak_search(q):
            if c:
                show = anihub_fetch_json(f"{ANIKOTO_BASE}/ajax/episode/list/{c['slug']}")
                if show and show.get("result"):
                    show_data = anihub_fetch_json(f"{ANIKOTO_BASE}/watch/{c['slug']}")
                    if show_data:
                        m = re.search(r'data-id="(\d+)"', show_data)
                        if m:
                            c["show_id"] = m.group(1)
                            return c
    return None


def ak_get_episodes(show_id: str) -> list:
    try:
        html = anihub_fetch_text(f"{ANIKOTO_BASE}/ajax/episode/list/{show_id}")
        eps = []
        for m in re.finditer(r'data-id="([^"]*)"[^>]*data-num="(\d+)"[^>]*data-sub="([^"]*)"[^>]*data-dub="([^"]*)"', html):
            ids, num, sub, dub = m.groups()
            eps.append({"number": int(num), "ids": ids, "has_sub": sub == "1", "has_dub": dub == "1"})
        eps.sort(key=lambda x: x["number"])
        return eps
    except Exception:
        return []


def ak_get_servers(ids: str, audio: str) -> list:
    try:
        html = anihub_fetch_json(f"{ANIKOTO_BASE}/ajax/server/list?servers={urlencode({'servers': ids}).split('=')[1]}")
        html = (html or {}).get("result", "")
        servers = []
        for m in re.finditer(r'<div class="type" data-type="([^"]+)">([\s\S]*?)</ul>\s*</div>', html):
            tn, body = m.groups()
            if tn != audio and not (audio == "sub" and tn == "hsub"):
                continue
            for li in re.finditer(r'<li\s+([^>]*data-link-id[^>]*)>([\s\S]*?)</li>', body):
                link_id = re.search(r'data-link-id="([^"]+)"', li.group(1))
                name = re.sub(r"<[^>]+>", "", li.group(2)).strip()
                if link_id:
                    servers.append({"link_id": link_id.group(1), "server_name": name, "audio": audio, "subtitle_type": "hardsub" if tn == "hsub" else "softsub"})
        return servers
    except Exception:
        return []


def ak_get_sources(link_id: str) -> str:
    try:
        r = anihub_fetch_json(f"{ANIKOTO_BASE}/ajax/server?get={urlencode({'get': link_id}).split('=')[1]}")
        return (r or {}).get("result", {}).get("url")
    except Exception:
        return None


# --- 2dhive Provider ---
def hv_get_episodes(mal_id: int) -> list:
    try:
        html = anihub_fetch_text(f"{TWODHIVE_BASE}/anime?anime={mal_id}")
        eps = []
        for m in re.finditer(r'/episode\?anime=' + str(mal_id) + r'&ep_num=(\d+)', html):
            eps.append(int(m.group(1)))
        return sorted(set(eps))
    except Exception:
        return []


def hv_get_servers(mal_id: int, ep_num: int, audio: str) -> list:
    servers = []
    # MegaPlay direct
    servers.append({"url": f"https://megaplay.buzz/stream/mal/{mal_id}/{ep_num}/{audio}", "server_name": "MegaPlay", "audio": audio})
    # Try hiAnime for sub
    if audio == "sub":
        try:
            r = anihub_fetch_json(f"{TWODHIVE_BASE}/api/hianime?mal_id={mal_id}&ep_num={ep_num}")
            if r and r.get("m3u8"):
                servers.append({"url": r["m3u8"], "server_name": "hiAnime", "audio": audio, "subtitle": r.get("subtitle")})
        except Exception:
            pass
    return servers


def _fetch_first_source(fetcher, servers: list) -> tuple[dict, str] | None:
    """Resolve alternative server links concurrently and return the first URL."""
    if not servers:
        return None
    executor = ThreadPoolExecutor(max_workers=min(len(servers), 3))
    futures = {executor.submit(fetcher, server["link_id"]): server
               for server in servers[:3]}
    try:
        for future in as_completed(futures):
            server = futures[future]
            try:
                result = future.result()
            except Exception:
                continue
            url = result.get("url") if isinstance(result, dict) else result
            if isinstance(url, str) and url.startswith(("http://", "https://")):
                return server, url
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return None


# --- Unified AniHub Stream Fetcher ---
async def resolve_anihub_course(
        tmdb_id: int, tmdb_season: int, tmdb_episode: int,
        client: httpx.AsyncClient) -> tuple[int | None, int]:
    """Resolve TMDB coordinates to an AniList course and course-local episode."""
    cache_key = f"vivarium:courses:{tmdb_id}"
    courses = _cache_get(_UPSTREAM_CACHE, cache_key, _COURSE_CACHE_TTL)
    try:
        if courses is None:
            response = await client.get(
                f"{VIV}/api/cours", params={"id": tmdb_id},
                timeout=httpx.Timeout(connect=3, read=8, write=3, pool=3))
            if response.status_code != 200:
                courses = []
            else:
                payload = response.json()
                courses = payload.get("cours") if isinstance(payload, dict) else None
                if not isinstance(courses, list):
                    courses = []
                else:
                    _cache_put(_UPSTREAM_CACHE, cache_key, courses)
    except (httpx.HTTPError, ValueError):
        _LOG.warning("AniHub course mapping unavailable for TMDB %s S%sE%s",
                     tmdb_id, tmdb_season, tmdb_episode)
        courses = []

    for course in courses:
        if not isinstance(course, dict) or not str(course.get("al", "")).isdigit():
            continue
        ranges = course.get("r")
        if not isinstance(ranges, list):
            continue
        course_episode = 0
        valid_course = True
        for item in ranges:
            if (not isinstance(item, list) or len(item) != 3
                    or not all(str(value).isdigit() for value in item)):
                valid_course = False
                break
            season, first, last = map(int, item)
            if season < 0 or first < 1 or last < first:
                valid_course = False
                break
            if (season == tmdb_season
                    and first <= tmdb_episode <= last):
                return int(course["al"]), (
                    course_episode + tmdb_episode - first + 1)
            course_episode += last - first + 1
        if not valid_course:
            continue
    fallback = await _resolve_simple_anime_course(
        tmdb_id, tmdb_season, tmdb_episode, client)
    return fallback if fallback else (None, tmdb_episode)


async def _resolve_simple_anime_course(
        tmdb_id: int, tmdb_season: int, tmdb_episode: int,
        client: httpx.AsyncClient) -> tuple[int, int] | None:
    """Resolve only verified one-season TMDB/AniList title matches."""
    if tmdb_season != 1 or tmdb_episode < 1:
        return None
    token = os.environ.get("TMDB_API_READ_ACCESS_TOKEN")
    if not token:
        _LOG.warning(
            "Cannot resolve AniHub single-season mapping for TMDB %s: "
            "TMDB_API_READ_ACCESS_TOKEN is not configured",
            tmdb_id)
        return None

    cache_key = f"anihub:simple-course:{tmdb_id}:{tmdb_episode}"
    cached = _cache_get(_UPSTREAM_CACHE, cache_key, _COURSE_CACHE_TTL)
    if cached is not None:
        return cached

    headers = {"Authorization": f"Bearer {token}",
               "Accept": "application/json"}
    timeout = httpx.Timeout(connect=3, read=6, write=3, pool=3)
    try:
        show_response = await client.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}",
            headers=headers, timeout=timeout)
        if show_response.status_code != 200:
            return None
        show = show_response.json()
        if not isinstance(show, dict) or show.get("number_of_seasons") != 1:
            return None

        title = show.get("name")
        first_air_date = show.get("first_air_date") or ""
        if not isinstance(title, str) or not title.strip():
            return None
        year_match = re.match(r"^(\d{4})-", first_air_date)
        if not year_match:
            return None
        year = int(year_match.group(1))

        season_response = await client.get(
            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/1",
            headers=headers, timeout=timeout)
        if season_response.status_code != 200:
            return None
        season_data = season_response.json()
        episodes = season_data.get("episodes") if isinstance(
            season_data, dict) else None
        if (not isinstance(episodes, list)
                or any(not isinstance(item, dict)
                       or not isinstance(item.get("episode_number"), int)
                       for item in episodes)):
            return None
        ordered_episodes = sorted(
            episodes, key=lambda item: item["episode_number"])
        tmdb_position = next(
            (index + 1 for index, item in enumerate(ordered_episodes)
             if isinstance(item, dict)
             and item.get("episode_number") == tmdb_episode),
            None)
        if tmdb_position is None:
            return None

        anilist_response = await client.post(
            "https://graphql.anilist.co",
            json={
                "query": (
                    "query ($search: String!, $year: Int!) { "
                    "Page(page: 1, perPage: 10) { media(search: $search, "
                    "type: ANIME, seasonYear: $year) { id idMal episodes "
                    "seasonYear title { english romaji native } } } }"),
                "variables": {"search": title, "year": year},
            },
            headers={"Content-Type": "application/json",
                     "Accept": "application/json"},
            timeout=timeout)
        if anilist_response.status_code != 200:
            return None
        payload = anilist_response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        page = data.get("Page") if isinstance(data, dict) else None
        candidates = page.get("media") if isinstance(page, dict) else None
        if not isinstance(candidates, list):
            return None
        normalized_title = _normalize_title(title)
        matches = []
        for media in candidates:
            if not isinstance(media, dict):
                continue
            titles = media.get("title") or {}
            if not isinstance(titles, dict):
                continue
            names = (titles.get("english"), titles.get("romaji"),
                     titles.get("native"))
            if (media.get("seasonYear") == year
                    and media.get("episodes") == len(episodes)
                    and normalized_title in {
                        _normalize_title(name)
                        for name in names if isinstance(name, str)}):
                matches.append(media)
        if len(matches) != 1:
            _LOG.warning(
                "AniHub single-season mapping for TMDB %s was not unique "
                "(title=%r, year=%s, episodes=%s, matches=%s)",
                tmdb_id, title, year, len(episodes), len(matches))
            return None

        resolved = (int(matches[0]["id"]), tmdb_position)
        names = matches[0].get("title") or {}
        matched_title = next(
            (name for name in (names.get("english"), names.get("romaji"),
                               names.get("native"))
             if isinstance(name, str)
             and _normalize_title(name) == normalized_title),
            title)
        _cache_anihub(
            f"anilist:metadata:{resolved[0]}",
            {"title": matched_title, "mal_id": matches[0].get("idMal")})
        _cache_put(_UPSTREAM_CACHE, cache_key, resolved)
        return resolved
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        _LOG.warning(
            "Single-season AniHub mapping failed for TMDB %s S1E%s",
            tmdb_id, tmdb_episode, exc_info=True)
        return None


def _fetch_anihub_provider(
        provider: str,
        anilist_id: int, source_episode: int, audio: str,
        title: str, mal_id: int | None, first_only: bool) -> list:
    """Fetch one provider's source in a worker thread."""
    streams = []

    if provider == "aniwaves":
        results = aw_search(title)
        if results:
            best = results[0]
            eps = aw_get_episodes(anilist_id, best["site_id"], best["slug"])
            ep = next((item for item in eps
                       if item["number"] == source_episode), None)
            if ep and ((audio == "sub" and ep["has_sub"])
                       or (audio == "dub" and ep["has_dub"])):
                servers = aw_get_servers(
                    best["site_id"], ep["source_number"], audio)[:3]
                if first_only:
                    resolved = _fetch_first_source(aw_get_sources, servers)
                    if resolved:
                        server, url = resolved
                        streams.append({
                            "provider": "aniwaves",
                            "server": server["server_name"],
                            "url": url,
                            "type": "hls",
                            "quality": "auto",
                            "referer": (
                                f"{ANIWAVES_BASE}/watch/{best['slug']}/"
                                f"ep-{ep['source_number']}"),
                            "audio": audio,
                        })
                else:
                    for server in servers:
                        source = aw_get_sources(server["link_id"])
                        url = source.get("url") if isinstance(source, dict) else None
                        if url and url.startswith("http"):
                            streams.append({
                                "provider": "aniwaves",
                                "server": server["server_name"],
                                "url": url,
                                "type": "hls",
                                "quality": "auto",
                                "referer": (
                                    f"{ANIWAVES_BASE}/watch/{best['slug']}/"
                                    f"ep-{ep['source_number']}"),
                                "audio": audio,
                            })
    elif provider == "anikoto":
        resolved = ak_resolve(
            anilist_id, {"title": {"english": title}})
        if resolved and resolved.get("show_id"):
            eps = ak_get_episodes(resolved["show_id"])
            ep = next((item for item in eps
                       if item["number"] == source_episode), None)
            if ep and ((audio == "sub" and ep["has_sub"])
                       or (audio == "dub" and ep["has_dub"])):
                servers = ak_get_servers(ep["ids"], audio)[:3]
                if first_only:
                    source_result = _fetch_first_source(
                        ak_get_sources, servers)
                    if source_result:
                        server, url = source_result
                        streams.append({
                            "provider": "anikoto",
                            "server": server["server_name"],
                            "url": url,
                            "type": "hls",
                            "quality": "auto",
                            "referer": f"{ANIKOTO_BASE}/watch/{resolved['slug']}",
                            "audio": audio,
                            "subtitle_type": server.get("subtitle_type"),
                        })
                else:
                    for server in servers:
                        url = ak_get_sources(server["link_id"])
                        if url and url.startswith("http"):
                            streams.append({
                                "provider": "anikoto",
                                "server": server["server_name"],
                                "url": url,
                                "type": "hls",
                                "quality": "auto",
                                "referer": f"{ANIKOTO_BASE}/watch/{resolved['slug']}",
                                "audio": audio,
                                "subtitle_type": server.get("subtitle_type"),
                            })
    elif provider == "2dhive" and mal_id:
        if source_episode in hv_get_episodes(mal_id):
            for server in hv_get_servers(mal_id, source_episode, audio):
                if server["url"].startswith("http"):
                    streams.append({
                        "provider": "2dhive",
                        "server": server["server_name"],
                        "url": server["url"],
                        "type": "hls",
                        "quality": "auto",
                        "referer": (
                            f"{TWODHIVE_BASE}/episode?anime={mal_id}"
                            f"&ep_num={source_episode}"),
                        "audio": audio,
                        "subtitle": server.get("subtitle"),
                    })
    return streams


async def anihub_fetch_streams_by_tmdb(
        tmdb_id: int, tmdb_season: int, tmdb_episode: int,
        audio: str, client: httpx.AsyncClient,
        first_only: bool = False,
        resolved_course: tuple[int | None, int] | None = None) -> list:
    """Find AniHub sources using the TMDB coordinates supplied by the caller."""
    if resolved_course is None:
        resolved_course = await resolve_anihub_course(
            tmdb_id, tmdb_season, tmdb_episode, client)
    anilist_id, source_episode = resolved_course
    if anilist_id is None:
        return []

    metadata_key = f"anilist:metadata:{anilist_id}"
    metadata = _get_cached_anihub(metadata_key) or {}
    mapping = _get_anihub_mapping(anilist_id) or {}
    title = mapping.get("title") or metadata.get("title")
    mal_id = mapping.get("mal_id") or metadata.get("mal_id")
    if not title or not mal_id:
        try:
            response = await client.post(
                "https://graphql.anilist.co",
                json={
                    "query": (
                        "query ($id: Int!) { Media(id: $id, type: ANIME) "
                        "{ idMal title { english romaji native } } }"),
                    "variables": {"id": anilist_id},
                },
                headers={"Content-Type": "application/json",
                         "Accept": "application/json"},
                timeout=httpx.Timeout(connect=3, read=6, write=3, pool=3))
            if response.status_code == 200:
                data = response.json()
                media = ((data.get("data") or {}).get("Media") or {}) if isinstance(data, dict) else {}
                titles = media.get("title") or {}
                title = title or titles.get("english") or titles.get("romaji") or titles.get("native")
                mal_id = mal_id or media.get("idMal")
                if title and mal_id:
                    _cache_anihub(metadata_key, {
                        "title": title, "mal_id": mal_id})
        except (httpx.HTTPError, ValueError):
            _LOG.warning("AniList metadata unavailable for internal course %s",
                         anilist_id)

    if not title:
        return []
    provider_names = ["aniwaves", "anikoto", "2dhive"]
    tasks = {
        asyncio.create_task(asyncio.to_thread(
            _fetch_anihub_provider, provider, anilist_id, source_episode,
            audio, title, int(mal_id) if mal_id else None, first_only))
        for provider in provider_names
    }
    if not first_only:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        streams = []
        for provider, result in zip(provider_names, results):
            if isinstance(result, Exception):
                _LOG.warning("%s source lookup failed: %s", provider, result)
            else:
                streams.extend(result)
        return streams

    streams = []
    pending = tasks
    while pending:
        completed, pending = await asyncio.wait(
            pending, return_when=asyncio.FIRST_COMPLETED)
        for task in completed:
            try:
                streams.extend(task.result())
            except Exception:
                _LOG.warning("AniHub provider lookup failed", exc_info=True)
        if streams:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            return streams
    return streams


async def anihub_fetch_dual_audio_by_tmdb(
        tmdb_id: int, tmdb_season: int, tmdb_episode: int,
        client: httpx.AsyncClient, first_only: bool = False) -> list:
    """Resolve both AniHub audio profiles concurrently for one TMDB episode."""
    resolved_course = await resolve_anihub_course(
        tmdb_id, tmdb_season, tmdb_episode, client)
    if resolved_course[0] is None:
        return []
    results = await asyncio.gather(
        *(
            anihub_fetch_streams_by_tmdb(
                tmdb_id, tmdb_season, tmdb_episode, audio, client,
                first_only=first_only, resolved_course=resolved_course)
            for audio in ("sub", "dub")
        ),
        return_exceptions=True,
    )
    streams = []
    seen = set()
    for audio, result in zip(("sub", "dub"), results):
        if isinstance(result, Exception):
            _LOG.warning("AniHub %s audio lookup failed: %s", audio, result)
            continue
        for stream in result:
            if not isinstance(stream, dict):
                continue
            stream_audio = stream.get("audio")
            if stream_audio != audio:
                _LOG.warning(
                    "AniHub %s lookup returned mismatched audio metadata",
                    audio)
                continue
            key = (stream.get("url"), stream_audio)
            if key in seen:
                continue
            seen.add(key)
            streams.append(stream)
    return streams


def _filter_anihub_streams(
        streams: list, server: Optional[str], dub: bool,
        provider: Optional[str]) -> list:
    filtered = []
    provider_key = (provider or "").lower()
    for stream in streams:
        audio = stream.get("audio")
        if server and audio != ("dub" if server.lower() == "aster" else "sub"):
            continue
        if not server and dub and audio != "dub":
            continue
        source_provider = (stream.get("provider") or "").lower()
        if provider_key and provider_key not in (
                "anihub", source_provider, f"anihub:{source_provider}"):
            continue
        stream["server"] = "Aster" if audio == "dub" else "Vexa"
        stream["provider"] = f"anihub:{source_provider or 'unknown'}"
        filtered.append(stream)
    priority = {"aniwaves": 0, "anikoto": 1, "2dhive": 2}
    filtered.sort(key=lambda item: priority.get(
        item["provider"].split(":")[-1], 99))
    return filtered


def map_vivarium_to_tmdb(vivarium_id: int) -> dict:
    """Map a Vivarium internal ID to TMDB metadata.
    
    Args:
        vivarium_id: The Vivarium internal show ID
        
    Returns:
        dict with tmdb_id, title, type, seasons_tmdb, and other metadata
        None if ID not in mapping table
    """
    return VIVARIUM_ID_MAP.get(vivarium_id)


def get_api_params(vivarium_id: int, season: Optional[str] = None, 
                   episode: Optional[str] = None, dub: bool = False) -> dict:
    """Get API parameters for /api/vivarium endpoint.
    
    Args:
        vivarium_id: Vivarium internal show ID
        season: Season number (optional, defaults to "1")
        episode: Episode number (optional, defaults to "1")
        dub: Whether to request English dub audio
        
    Returns:
        dict with {id, type, s, e} parameters for API call
    """
    mapping = map_vivarium_to_tmdb(vivarium_id)
    if not mapping:
        return None
    
    content_type = mapping["type"]
    
    if content_type == "movie":
        return {"id": mapping["tmdb_id"], "type": "movie"}
    
    # For TV type - use provided season/episode or defaults
    s = season if season else "1"
    e = episode if episode else "1"
    
    return {
        "id": mapping["tmdb_id"],
        "type": "tv",
        "s": s,
        "e": e,
    }


def get_stream_server_filters(dub: bool = False, server: Optional[str] = None) -> dict:
    """Get stream filtering parameters based on dub/sub preference.
    
    Args:
        dub: Whether to filter for English dub
        server: Server filter ("aster" for dub, "vexa" for sub, None for both)
        
    Returns:
        dict suitable for passing to filter_streams() or API params
    """
    from api import classify_streams, filter_streams, is_dub, has_english_subtitles
    
    # The actual filtering is done in the API endpoints
    # This function provides the logic description
    if server == "aster":
        return {"server_key": "aster"}
    elif server == "vexa":
        return {"server_key": "vexa"}
    elif dub:
        return {"dub": True}
    else:
        return {"dub": False}


# Keep reference to these for endpoint use
__all__ = [
    "VIVARIUM_ID_MAP",
    "map_vivarium_to_tmdb", 
    "get_api_params",
    "get_stream_server_filters",
]

_LOG = logging.getLogger("vivarium.performance")
_HEALTH_LOG = logging.getLogger("vivarium.health")
_HEALTH_LOG.setLevel(logging.INFO)
_HEALTH_LOG.propagate = False
if not _HEALTH_LOG.handlers:
    _health_handler = logging.StreamHandler()
    _health_handler.setFormatter(logging.Formatter("%(message)s"))
    _HEALTH_LOG.addHandler(_health_handler)
_CACHE = OrderedDict()  # (id,type,s,e,server,dub,provider,race) -> (at, result)
_CACHE_TTL = 30
_UPSTREAM_CACHE = OrderedDict()  # path -> (at, payload)
_UPSTREAM_CACHE_TTL = 30
_COURSE_CACHE_TTL = 1800
_CACHE_LIMIT = 512
_CACHE_LOCK = threading.Lock()
_METRICS_LOCK = threading.Lock()
_STARTED_AT = time.time()
_METRICS = {
    "requests_total": 0,
    "status_codes": {},
    "paths": {},
    "total_latency_ms": 0.0,
    "max_latency_ms": 0.0,
    "last_request_at": None,
    "lookups_total": 0,
    "empty_lookups": 0,
}


def sign_request(path_qs: str):
    return path_qs, vc.sign(path_qs), {"vg": vc.VG}


def require_admin(request: Request):
    password = os.environ.get("VIVARIUM_ADMIN_PASSWORD")
    if not password:
        raise HTTPException(
            status_code=503, detail="Admin access is not configured")

    scheme, separator, credential = request.headers.get(
        "authorization", "").partition(" ")
    if (not separator or scheme.lower() != "bearer"
            or not hmac.compare_digest(
                credential.strip().encode("utf-8"),
                password.encode("utf-8"))):
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing admin password",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _cache_get(cache, key, ttl):
    now = time.monotonic()
    with _CACHE_LOCK:
        entry = cache.get(key)
        if entry is None:
            return None
        if now - entry[0] >= ttl:
            del cache[key]
            return None
        cache.move_to_end(key)
        return entry[1]


def _cache_put(cache, key, value):
    with _CACHE_LOCK:
        cache[key] = (time.monotonic(), value)
        cache.move_to_end(key)
        while len(cache) > _CACHE_LIMIT:
            cache.popitem(last=False)


def _log_timing(stages):
    _LOG.info("scrape_timing_ms %s", " ".join(
        f"{name}={value:.2f}" for name, value in stages.items()))


def _record_lookup(empty: bool):
    with _METRICS_LOCK:
        _METRICS["lookups_total"] += 1
        if empty:
            _METRICS["empty_lookups"] += 1


def _usable_hls_stream(stream: dict) -> bool:
    url = stream.get("url")
    if str(stream.get("type") or "").lower() != "hls" or not isinstance(url, str):
        return False
    return url.strip().lower().startswith(("http://", "https://"))


def _no_sources_response():
    return JSONResponse(status_code=404, content={
        "status": 404, "result": "",
        "error": "No usable HLS sources found",
        "code": "no_sources",
        "hint": "Try again later or check /api/health-vivarium for provider status.",
    })


class AdminVgBody(BaseModel):
    vg: Optional[str] = None


def build_qs(id: str, type: str, s: Optional[str], e: Optional[str]) -> str:
    if type not in ("movie", "tv"):
        raise ValueError("type must be movie|tv")
    if type == "movie":
        return f"/api/e?{urlencode({'id': id, 'type': 'movie'})}"
    if not s or not e:
        raise ValueError("tv requires s and e")
    return f"/api/e?{urlencode({'id': id, 'type': type, 's': s, 'e': e})}"


def _link_query_value(query: dict, key: str) -> Optional[str]:
    values = query.get(key)
    return values[-1] if values else None


def parse_media_url(media_url: str) -> dict:
    """Parse a TMDB episode/movie URL or a Vivarium show URL."""
    try:
        parsed = urlsplit(media_url)
    except ValueError as ex:
        raise ValueError("Invalid media URL") from ex
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP(S) media link")
    if parsed.username or parsed.password:
        raise ValueError("Credentials are not allowed in media URLs")

    host = parsed.hostname.lower()
    segments = [part for part in parsed.path.split("/") if part]
    query = parse_qs(parsed.query)
    if host in ("themoviedb.org", "www.themoviedb.org", "m.themoviedb.org"):
        route_index = next(
            (index for index, part in enumerate(segments)
             if part in ("movie", "tv")),
            None)
        if route_index is None or route_index + 1 >= len(segments):
            raise ValueError("TMDB URL must contain /movie/<id> or /tv/<id>")
        content_type = segments[route_index]
        id_match = re.match(r"^(\d+)(?:-|$)", segments[route_index + 1])
        if not id_match:
            raise ValueError("TMDB URL does not contain a numeric media id")
        season = _link_query_value(query, "s")
        episode = _link_query_value(query, "e")
        if content_type == "tv":
            for index, part in enumerate(segments[route_index + 2:],
                                         start=route_index + 2):
                if part == "season" and index + 1 < len(segments):
                    season = segments[index + 1]
                elif part == "episode" and index + 1 < len(segments):
                    episode = segments[index + 1]
        return {
            "id": id_match.group(1), "type": content_type,
            "s": season, "e": episode,
        }

    if host not in ("vivarium.su", "www.vivarium.su"):
        raise ValueError("URL host must be themoviedb.org or vivarium.su")
    if len(segments) < 2 or segments[0] not in ("s", "m"):
        raise ValueError("Vivarium URL must use /s/<slug-id> or /m/<slug-id>")
    id_match = re.search(r"-(\d+)$", segments[1])
    if not id_match:
        raise ValueError("Vivarium URL does not contain a numeric media id")
    return {
        "id": id_match.group(1),
        "type": "tv" if segments[0] == "s" else "movie",
        "s": _link_query_value(query, "s"),
        "e": _link_query_value(query, "e"),
    }


async def resolve_media_url(media_url: str, season: Optional[str],
                            episode: Optional[str], client) -> dict:
    """Resolve URL parameters to the TMDB coordinates Vivarium's API expects."""
    media = parse_media_url(media_url)
    media["s"] = season or media["s"]
    media["e"] = episode or media["e"]
    if media["type"] == "movie":
        return media

    if (media["s"] is not None
            and (not re.fullmatch(r"\d+", media["s"])
                 or int(media["s"]) < 0)):
        raise ValueError("Season number (s) must be a non-negative integer")
    if (media["e"] is not None
            and (not re.fullmatch(r"\d+", media["e"])
                 or int(media["e"]) < 1)):
        raise ValueError("Episode number (e) must be a positive integer")

    return media


# FOREGROUND servers. Only these two are shown; every real provider
# (Febbox, Hermes, ...) is still fetched in the background and its streams
# are classified into one of these two.
SERVERS = [
    {"server": "Aster", "audio": "dub",
     "description": "English dub anime",
     "status": "configured", "availability": "per_title"},
    {"server": "Vexa", "audio": "japanese",
     "description": "Japanese audio anime with English subtitles",
     "status": "configured", "availability": "per_title"},
]


def is_dub(s: dict) -> bool:
    audio = str(s.get("audio") or "").lower()
    audio_language = " ".join(str(s.get(key) or "") for key in (
        "audio_language", "audioLanguage"))
    return (audio in ("dub", "english", "eng", "en")
            or "dub" in str(s.get("quality") or "").lower()
            or bool(re.search(r"\b(?:english|eng|en)\b",
                              audio_language, re.I)))


def is_japanese_audio(s: dict) -> bool:
    language = " ".join(str(s.get(key) or "") for key in (
        "audio_language", "audioLanguage", "original_language"))
    if language:
        return bool(re.search(
            r"\b(?:japanese|jpn|ja|jap)\b", language, re.I))
    return (str(s.get("audio") or "").lower() in ("", "sub", "japanese")
            and not is_dub(s))


def has_english_subtitles(stream: dict) -> bool:
    subtitles = stream.get("subs")
    if not isinstance(subtitles, list):
        return False
    for subtitle in subtitles:
        if not isinstance(subtitle, dict):
            continue
        labels = " ".join(str(subtitle.get(key) or "")
                          for key in ("lang", "language", "label"))
        if re.search(r"\b(?:english|eng|en)\b", labels, re.I):
            return True
    return False


def classify_streams(data: dict):
    """Split background provider streams into the two foreground servers."""
    streams = data.get("streams", []) or []
    aster = sorted([x for x in streams if is_dub(x)],
                   key=lambda x: x.get("rank", 0), reverse=True)
    vexa = sorted([x for x in streams
                   if not is_dub(x) and is_japanese_audio(x)
                   and has_english_subtitles(x)],
                  key=lambda x: (bool(x.get("subs")), x.get("rank", 0)),
                  reverse=True)
    return {"aster": aster, "vexa": vexa}


def filter_streams(data: dict, dub: bool = False, provider: Optional[str] = None,
                   server: Optional[str] = None):
    streams = data.get("streams", []) or []
    server_key = (server or "").lower()
    provider_key = (provider or "").lower()

    filtered = []
    for item in streams:
        if server_key == "aster" and not is_dub(item):
            continue
        if server_key == "vexa" and (
                is_dub(item) or not is_japanese_audio(item)
                or not has_english_subtitles(item)):
            continue
        elif not server_key and dub and not is_dub(item):
            continue
        elif not server_key and not dub and not (
                is_dub(item) or (
                    is_japanese_audio(item) and has_english_subtitles(item))):
            continue
        if provider_key and (item.get("provider") or "").lower() != provider_key:
            continue
        item["server"] = "Aster" if is_dub(item) else "Vexa"
        filtered.append(item)

    if server_key == "vexa":
        filtered.sort(key=lambda x: (bool(x.get("subs")), x.get("rank", 0)), reverse=True)
    return {"streams": filtered, "subtitles": data.get("subtitles", []),
            "qualities": quality_list(filtered)}


def _vivarium_profile_fallback(
        payload: dict, server: Optional[str], dub: bool,
        provider: Optional[str]) -> dict | None:
    requested = (server or ("aster" if dub else "")).lower()
    if requested not in ("aster", "vexa"):
        return None

    fallback_server = "vexa" if requested == "aster" else "aster"
    result = filter_streams(payload, provider=provider, server=fallback_server)
    result["streams"] = [
        stream for stream in result["streams"]
        if _usable_hls_stream(stream)]
    if not result["streams"]:
        return None

    result["qualities"] = quality_list(result["streams"])
    result["fallback"] = {
        "requested_server": requested,
        "served_server": fallback_server,
        "reason": (
            f"No usable {requested} Vivarium stream was found for this "
            f"episode; returned the available {fallback_server} Vivarium "
            "stream instead."),
    }
    return result


def parse_quality(s: dict) -> dict:
    """Normalize one stream's quality label into comparable fields."""
    q = (s.get("quality") or "").strip() or "Auto"
    m = re.search(r"(\d{3,4})\s*p", q, re.I)
    height = int(m.group(1)) if m else 0
    return {"label": q, "height": height,
            "dub": is_dub(s), "hevc": bool(s.get("hevc")),
            "hdr": bool(s.get("hdr")), "provider": s.get("provider"),
            "type": s.get("type")}


def quality_list(streams) -> list:
    """Every available quality, de-duplicated, best first."""
    seen = {}
    for s in streams or []:
        p = parse_quality(s)
        key = (p["label"].lower(), (p["provider"] or "").lower())
        if key not in seen:
            seen[key] = p
    return sorted(seen.values(),
                  key=lambda p: (p["height"], p["label"]), reverse=True)


def wants_for(server_or_dub, s: dict) -> bool:
    if str(s.get("type") or "").lower() != "hls":
        return False
    if server_or_dub in ("aster", True):
        return is_dub(s)
    if server_or_dub in ("vexa",):
        return (not is_dub(s) and is_japanese_audio(s)
                and has_english_subtitles(s))
    return is_dub(s) or (
        is_japanese_audio(s) and has_english_subtitles(s))


async def _cached_upstream(client, path_qs: str, timings: dict):
    cache_started = time.perf_counter()
    payload = _cache_get(_UPSTREAM_CACHE, path_qs, _UPSTREAM_CACHE_TTL)
    timings["upstream_cache_lookup"] = (time.perf_counter() - cache_started) * 1000
    if payload is not None:
        return payload

    _, headers, cookies = await asyncio.to_thread(sign_request, path_qs)
    started = time.perf_counter()
    response = await client.get(
        f"{VIV}{path_qs}", headers=headers, cookies=cookies,
        timeout=httpx.Timeout(connect=5, read=20, write=5, pool=5))
    timings["request"] = (time.perf_counter() - started) * 1000
    if response.status_code != 200:
        raise RuntimeError(f"Upstream fetch failed: {response.status_code}")

    started = time.perf_counter()
    payload = response.json()
    timings["parse"] = (time.perf_counter() - started) * 1000
    streams = payload.get("streams", []) if isinstance(payload, dict) else []
    if any(isinstance(stream, dict) and _usable_hls_stream(stream)
           for stream in streams or []):
        _cache_put(_UPSTREAM_CACHE, path_qs, payload)
    return payload


async def race_streams(client, path_qs: str, server_or_dub, provider, timings: dict):
    """Parallel sub-server race: /api/es fans out to every provider at once,
    first `source` event fitting the foreground server wins the playback."""
    import json
    es_qs = path_qs.replace("/api/e", "/api/es", 1)
    _, headers, cookies = await asyncio.to_thread(sign_request, es_qs)
    request_started = time.perf_counter()
    async with client.stream(
            "GET", f"{VIV}{es_qs}", headers=headers, cookies=cookies) as response:
        timings["request"] = (time.perf_counter() - request_started) * 1000
        if response.status_code != 200:
            raise RuntimeError(f"Upstream search failed: {response.status_code}")

        event_name = ""
        data_lines = []
        async for line in response.aiter_lines():
            if line.startswith("event:"):
                event_name = line[6:].strip()
            elif line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
            elif not line:
                if event_name == "done":
                    timings["search"] = (
                        time.perf_counter() - request_started) * 1000
                    return None
                if event_name == "source" and data_lines:
                    started = time.perf_counter()
                    try:
                        data = json.loads("\n".join(data_lines))
                    except (json.JSONDecodeError, TypeError):
                        data = {}
                    timings["parse"] = timings.get("parse", 0.0) + (
                        time.perf_counter() - started) * 1000

                    started = time.perf_counter()
                    streams = data.get("streams") if isinstance(data, dict) else None
                    for stream in streams or []:
                        if (isinstance(stream, dict)
                                and wants_for(server_or_dub, stream)
                                and _usable_hls_stream(stream)
                                and (not provider or
                                     (stream.get("provider") or "").lower()
                                     == provider.lower())):
                            timings["extract"] = (time.perf_counter() - started) * 1000
                            timings["search"] = (
                                time.perf_counter() - request_started) * 1000
                            return stream
                    timings["extract"] = timings.get("extract", 0.0) + (
                        time.perf_counter() - started) * 1000
                event_name = ""
                data_lines = []
    timings["search"] = (time.perf_counter() - request_started) * 1000
    return None


@asynccontextmanager
async def lifespan(app):
    limits = httpx.Limits(max_connections=100, max_keepalive_connections=40,
                          keepalive_expiry=30)
    timeout = httpx.Timeout(connect=5, read=25, write=5, pool=5)
    async with httpx.AsyncClient(
            http2=True, limits=limits, timeout=timeout, headers=HEADERS) as client:
        app.state.http = client
        yield


app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def profile_request(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    with _METRICS_LOCK:
        _METRICS["requests_total"] += 1
        status = str(response.status_code)
        _METRICS["status_codes"][status] = (
            _METRICS["status_codes"].get(status, 0) + 1)
        route = request.scope.get("route")
        path = route.path if route else "unmatched"
        _METRICS["paths"][path] = _METRICS["paths"].get(path, 0) + 1
        _METRICS["total_latency_ms"] += elapsed_ms
        _METRICS["max_latency_ms"] = max(_METRICS["max_latency_ms"], elapsed_ms)
        _METRICS["last_request_at"] = datetime.datetime.now(
            datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    if request.url.path == "/health":
        _HEALTH_LOG.info(
            "health_probe timestamp=%s path=%s status=%d latency_ms=%.2f cf_ray=%s",
            datetime.datetime.now(datetime.timezone.utc).isoformat(
                timespec="milliseconds").replace("+00:00", "Z"),
            request.url.path,
            response.status_code,
            elapsed_ms,
            request.headers.get("cf-ray", "-"),
        )
    if request.url.path == "/api/vivarium":
        _LOG.info("scrape_response_ms %.2f", elapsed_ms)
    return response
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


class DecBody(BaseModel):
    response: Optional[dict] = None
    dub: bool = False
    provider: Optional[str] = None
    server: Optional[str] = None


@app.get("/")
def root():
    return {"status": "ok", "service": "vivarium-api"}

@app.get("/health")
async def health():
    return {"status": "healthy"}

@app.get("/api/servers")
def servers():
    return {"status": 200, "result": {"servers": SERVERS}}


@app.get("/api/health-vivarium")
async def health_vivarium(request: Request):
    try:
        response = await request.app.state.http.get(
            f"{VIV}/api/health", timeout=httpx.Timeout(
                connect=5, read=15, write=5, pool=5))
    except httpx.HTTPError as ex:
        return {"status": 502, "result": "", "error": f"Upstream health failed: {ex}"}
    if response.status_code != 200:
        return {"status": response.status_code, "result": "", "error": "Upstream health failed"}
    try:
        upstream_health = response.json()
    except ValueError:
        return {"status": 502, "result": "",
                "error": "Upstream health returned invalid JSON"}
    if not isinstance(upstream_health, dict):
        return {"status": 502, "result": "",
                "error": "Upstream health returned an invalid payload"}
    return {
        "status": 200,
        "result": {
            **upstream_health,
            "upstream_status": "reachable",
            "profiles": SERVERS,
            "health_scope": (
                "Profiles are configured; source availability is checked "
                "per title and episode."),
        },
    }


@app.get("/api/enc-vivarium", dependencies=[Depends(require_admin)])
def enc_vivarium(
    id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    e: Optional[str] = Query(None),
):
    if not id or not type:
        return {"status": 400, "result": "", "error": "Expected query: id, type",
                "hint": "GET: /api/enc-vivarium?id=[tmdb_or_internal]&type=[movie|tv]&s=[season]&e=[episode]"}
    if type not in ("movie", "tv"):
        return {"status": 400, "result": "", "error": "Invalid type",
                "hint": "type must be movie|tv"}
    try:
        path = build_qs(id, type, s, e)
    except ValueError as ex:
        return {"status": 400, "result": "", "error": str(ex)}
    path, headers, cookies = sign_request(path)
    return {"status": 200, "result": {"path": path, "headers": headers, "cookies": cookies,
                                      "url": f"{VIV}{path}"}}


@app.post("/api/dec-vivarium")
def dec_vivarium(body: DecBody):
    if not isinstance(body.response, dict) or "streams" not in body.response:
        return {"status": 400, "result": "", "error": "Expected body: response, [dub, provider, server]",
                "hint": "POST: { 'response': <raw /api/e JSON>, 'dub': false }"}
    if body.server and body.server.lower() not in ("aster", "vexa"):
        return {"status": 400, "result": "", "error": "Invalid server",
                "hint": "server must be aster|vexa"}
    result = filter_streams(body.response, body.dub, body.provider, body.server)
    result["streams"] = [stream for stream in result["streams"]
                         if _usable_hls_stream(stream)]
    result["qualities"] = quality_list(result["streams"])
    if not result["streams"]:
        return _no_sources_response()
    return {"status": 200, "result": result}


@app.get("/api/vivarium")
async def vivarium(
    request: Request,
    id: Optional[str] = Query(None),
    type: Optional[str] = Query(None),
    url: Optional[str] = Query(None),
    s: Optional[str] = Query(None),
    e: Optional[str] = Query(None),
    dub: bool = Query(False),
    provider: Optional[str] = Query(None),
    server: Optional[str] = Query(None),
    race: bool = Query(True),
):
    if server and server.lower() not in ("aster", "vexa"):
        return {"status": 400, "result": "", "error": "Invalid server",
                "hint": "server must be aster|vexa"}
    if url and (id or type):
        return {"status": 400, "result": "",
                "error": "Use either url or id and type, not both"}
    client = request.app.state.http
    if url:
        try:
            media = await resolve_media_url(url, s, e, client)
        except ValueError as ex:
            return {"status": 400, "result": "", "error": str(ex)}
        except RuntimeError as ex:
            return {"status": 502, "result": "", "error": str(ex)}
        id, type, s, e = media["id"], media["type"], media["s"], media["e"]
    elif not id or not type:
        return {"status": 400, "result": "", "error": "Expected query: id, type",
            "hint": "GET: /api/vivarium?url=[TMDB_or_Vivarium_link] or /api/vivarium?id=[tmdb_or_internal]&type=[movie|tv]&s=[season]&e=[episode]&server=[aster|vexa]"}
    try:
        path = build_qs(id, type, s, e)
    except ValueError as ex:
        return {"status": 400, "result": "", "error": str(ex)}
    key = (id, type, s, e, (server or "").lower(), bool(dub),
           (provider or "").lower(), bool(race))
    cache_started = time.perf_counter()
    res = _cache_get(_CACHE, key, _CACHE_TTL)
    cache_lookup_ms = (time.perf_counter() - cache_started) * 1000
    if res is not None:
        if not res.get("streams"):
            with _CACHE_LOCK:
                _CACHE.pop(key, None)
            _record_lookup(True)
            return _no_sources_response()
        started = time.perf_counter()
        res = dict(res)
        res["cached"] = True
        result = {"status": 200, "result": res}
        _record_lookup(not bool(res.get("streams")))
        _log_timing({"cache_lookup": cache_lookup_ms,
                     "response_generation": (time.perf_counter() - started) * 1000})
        return result
    timings = {"cache_lookup": cache_lookup_ms}
    client = request.app.state.http
    tmdb_id = int(id)
    tmdb_season = int(s) if s else 1
    tmdb_episode = int(e) if e else 1
    anihub_race_completed = False
    anihub_race_streams = []
    if race:
        vivarium_task = asyncio.create_task(race_streams(
            client, path, server.lower() if server else (True if dub else None),
            provider, timings))
        pending = {vivarium_task}
        if type == "tv":
            anihub_task = asyncio.create_task(anihub_fetch_dual_audio_by_tmdb(
                tmdb_id, tmdb_season, tmdb_episode, client,
                first_only=True))
            pending.add(anihub_task)
        while pending:
            completed, pending = await asyncio.wait(
                pending, return_when=asyncio.FIRST_COMPLETED)

            # Vivarium is the preferred source even when AniHub resolves first.
            # Process its result first when both tasks complete together.
            completed_tasks = sorted(
                completed, key=lambda task: task is not vivarium_task)
            for task in completed_tasks:
                try:
                    candidate = task.result()
                except Exception:
                    _LOG.warning("A source race participant failed",
                                 exc_info=True)
                    continue
                if task is vivarium_task:
                    if candidate:
                        res = filter_streams(
                            {"streams": [candidate], "subtitles": []},
                            dub, provider, server)
                        res["streams"] = [
                            stream for stream in res["streams"]
                            if _usable_hls_stream(stream)]
                        res["qualities"] = quality_list(res["streams"])
                    else:
                        continue
                    if not res["streams"]:
                        continue
                    for loser in pending:
                        loser.cancel()
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    _cache_put(_CACHE, key, res)
                    _record_lookup(False)
                    result = {"status": 200, "result": res}
                    _log_timing(timings)
                    return result
                else:
                    anihub_race_completed = True
                    anihub_race_streams = candidate
                    timings["anihub_ready"] = True
        # Neither source returned a matching HLS stream; use the full response path.
    try:
        payload = await _cached_upstream(client, path, timings)
    except (RuntimeError, httpx.HTTPError) as ex:
        payload = None
    if payload:
        started = time.perf_counter()
        res = filter_streams(payload, dub, provider, server)
        res["streams"] = [stream for stream in res["streams"]
                          if _usable_hls_stream(stream)]
        res["qualities"] = quality_list(res["streams"])
        if res["streams"]:
            timings["source_extraction"] = (time.perf_counter() - started) * 1000
            _cache_put(_CACHE, key, res)
            _record_lookup(False)
            started = time.perf_counter()
            result = {"status": 200, "result": res}
            timings["response_generation"] = (time.perf_counter() - started) * 1000
            _log_timing(timings)
            return result
        profile_fallback = _vivarium_profile_fallback(
            payload, server, dub, provider)
        if profile_fallback:
            timings["profile_fallback"] = True
            _cache_put(_CACHE, key, profile_fallback)
            _record_lookup(False)
            result = {"status": 200, "result": profile_fallback}
            _log_timing(timings)
            return result
    # Vivarium upstream returned no usable streams - try AniHub fallback
    anihub_streams = []
    if type == "tv":
        anihub_streams = (
            anihub_race_streams if anihub_race_completed
            else await anihub_fetch_dual_audio_by_tmdb(
                tmdb_id, tmdb_season, tmdb_episode, client,
                first_only=True))
    if anihub_streams:
        filtered = _filter_anihub_streams(anihub_streams, server, dub, provider)
        if filtered:
            res = {"streams": filtered, "subtitles": [], "qualities": quality_list(filtered)}
            timings["anihub_fallback"] = True
            _record_lookup(False)
            started = time.perf_counter()
            result = {"status": 200, "result": res}
            timings["response_generation"] = (time.perf_counter() - started) * 1000
            _log_timing(timings)
            return result
    _record_lookup(True)
    _log_timing(timings)
    return _no_sources_response()
    timings["response_generation"] = (time.perf_counter() - started) * 1000
    _log_timing(timings)
    return result


@app.get("/api/status")
def api_status():
    with _METRICS_LOCK:
        request_count = _METRICS["requests_total"]
        avg_latency = (_METRICS["total_latency_ms"] / request_count
                       if request_count else 0.0)
        runtime = {
            "started_at": datetime.datetime.fromtimestamp(
                _STARTED_AT, datetime.timezone.utc).isoformat(
                    timespec="seconds").replace("+00:00", "Z"),
            "uptime_seconds": round(time.time() - _STARTED_AT, 2),
            "last_request_at": _METRICS["last_request_at"],
            "requests_total": request_count,
            "average_latency_ms": round(avg_latency, 2),
            "max_latency_ms": round(_METRICS["max_latency_ms"], 2),
            "lookups_total": _METRICS["lookups_total"],
            "empty_lookups": _METRICS["empty_lookups"],
        }
    with _CACHE_LOCK:
        cache = {"response_entries": len(_CACHE),
                 "upstream_entries": len(_UPSTREAM_CACHE)}
    with vc._NONCES_LOCK:
        nonce_count = len(vc._NONCES)
    signing_ready = bool(vc.U_HEX and vc.X_CV and vc._exp is not None)
    vg_expiry = vc._VG_EXPIRY["at"]
    if vg_expiry and vg_expiry <= time.time():
        signing_ready = False
    return {"status": 200, "result": {
        "service": "vivarium-api",
        "state": "ready" if signing_ready else "degraded",
        "runtime": runtime,
        "cache": cache,
        "crypto": {
            "vg_source": vc.KEY_SOURCE["vg"],
            "vg_expiry_utc": datetime.datetime.fromtimestamp(
                vg_expiry, datetime.timezone.utc).isoformat(timespec="seconds")
                if vg_expiry else None,
            "crypto_source": vc.KEY_SOURCE["crypto"],
            "wasm_ready": vc._exp is not None,
            "signing_ready": signing_ready,
            "nonce_pool_size": nonce_count,
            "bootstrap_running": vc._BOOT["running"],
            "bootstrap_ok": vc._BOOT["ok"],
        },
    }}


@app.get("/api/status/metrics")
def api_status_metrics():
    with _METRICS_LOCK:
        request_count = _METRICS["requests_total"]
        return {"status": 200, "result": {
            "requests_total": request_count,
            "status_codes": dict(_METRICS["status_codes"]),
            "paths": dict(_METRICS["paths"]),
            "average_latency_ms": round(
                _METRICS["total_latency_ms"] / request_count, 2)
                if request_count else 0.0,
            "max_latency_ms": round(_METRICS["max_latency_ms"], 2),
            "last_request_at": _METRICS["last_request_at"],
            "lookups_total": _METRICS["lookups_total"],
            "empty_lookups": _METRICS["empty_lookups"],
        }}


@app.get("/api/status/cache")
def api_status_cache():
    with _CACHE_LOCK:
        return {"status": 200, "result": {
            "response_entries": len(_CACHE),
            "upstream_entries": len(_UPSTREAM_CACHE),
            "entry_limit": _CACHE_LIMIT,
            "response_ttl_seconds": _CACHE_TTL,
            "upstream_ttl_seconds": _UPSTREAM_CACHE_TTL,
        }}


@app.get("/api/status/crypto")
def api_status_crypto():
    expiry = vc._VG_EXPIRY["at"]
    with vc._NONCES_LOCK:
        nonce_count = len(vc._NONCES)
    return {"status": 200, "result": {
        "vg_source": vc.KEY_SOURCE["vg"],
        "vg_expiry_utc": datetime.datetime.fromtimestamp(
            expiry, datetime.timezone.utc).isoformat(timespec="seconds")
            if expiry else None,
        "crypto_source": vc.KEY_SOURCE["crypto"],
        "wasm_ready": vc._exp is not None,
        "wasm_url": vc.WASM_URL,
        "nonce_pool_size": nonce_count,
        "bootstrap": {
            "running": vc._BOOT["running"],
            "ok": vc._BOOT["ok"],
            "last": vc._BOOT["last"],
        },
    }}


@app.get("/api/admin/status", dependencies=[Depends(require_admin)])
def admin_status():
    exp = vc._VG_EXPIRY["at"]
    return {"status": 200, "result": {
        "vg_source": vc.KEY_SOURCE["vg"],
        "vg_expiry_utc": (datetime.datetime.fromtimestamp(exp, datetime.timezone.utc).isoformat()
                          if exp else None),
        "crypto_source": vc.KEY_SOURCE["crypto"],
        "crypto_bootstrap": vc._BOOT,
        "wasm_url": vc.WASM_URL,
    }}


@app.post("/api/admin/vg", dependencies=[Depends(require_admin)])
def admin_vg(body: AdminVgBody):
    if not body.vg or len(body.vg.strip()) < 20:
        return {"status": 400, "result": "", "error": "Expected body: vg",
                "hint": "POST: { 'vg': '<vg cookie value from browser>' }"}
    vc.set_vg(body.vg, "admin")
    ok = vc.ensure_vg()
    return {"status": 200 if ok else 502,
            "result": {"vg_source": vc.KEY_SOURCE["vg"], "usable": ok}}
