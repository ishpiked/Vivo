"""Portable reference client for the hosted Vivarium Stream API.

Set TMDB_API_READ_ACCESS_TOKEN to enable title search. This token is sent to
TMDB only; stream lookups go to VIVARIUM_API_BASE_URL.
"""
import argparse
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


DEFAULT_API_BASE_URL = "https://kitsu-backend-2mbi.onrender.com"
TMDB_API_BASE_URL = "https://api.themoviedb.org/3"
USER_AGENT = "VivariumReferenceClient/1.0"


class APIError(RuntimeError):
    """An HTTP or API-level failure with a user-facing message."""


def _get_json(url, headers=None, timeout=30):
    request_headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read()
    except HTTPError as ex:
        try:
            details = ex.read().decode("utf-8", errors="replace")
        except OSError:
            details = ""
        raise APIError(f"HTTP {ex.code} from {url}: {details or ex.reason}") from ex
    except (URLError, TimeoutError) as ex:
        raise APIError(f"Could not reach {url}: {ex}") from ex

    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as ex:
        raise APIError(f"Expected JSON from {url}") from ex


def _api_base_url(base_url=None):
    base = base_url or os.environ.get(
        "VIVARIUM_API_BASE_URL", DEFAULT_API_BASE_URL)
    return base.rstrip("/")


def search_titles(title, year=None, media_type="multi", tmdb_token=None):
    """Search TMDB for shows/movies and return its result list.

    Title search is performed by TMDB, not by the Vivarium API. A TMDB API
    Read Access Token can be passed explicitly or set in the environment.
    """
    token = tmdb_token or os.environ.get("TMDB_API_READ_ACCESS_TOKEN")
    if not token:
        raise APIError(
            "Set TMDB_API_READ_ACCESS_TOKEN to search titles, or use an "
            "existing TMDB/Vivarium URL with get_streams_by_url().")
    if media_type not in ("multi", "tv", "movie"):
        raise ValueError("media_type must be multi, tv, or movie")

    params = {"query": title, "include_adult": "false"}
    if year:
        params["year" if media_type == "movie" else "first_air_date_year"] = str(year)
    endpoint = "search/multi" if media_type == "multi" else f"search/{media_type}"
    data = _get_json(
        f"{TMDB_API_BASE_URL}/{endpoint}?{urlencode(params)}",
        headers={"Authorization": f"Bearer {token}"})
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise APIError("TMDB returned an invalid search response")
    return results


def _stream_result(data):
    if not isinstance(data, dict):
        raise APIError("Vivarium API returned an invalid response")
    result = data.get("result")
    if data.get("status") != 200 or not isinstance(result, dict):
        raise APIError(data.get("error") or "Stream lookup failed")
    return result


def get_streams_by_url(media_url, server=None, race=True, base_url=None):
    """Look up streams for a TMDB or Vivarium movie/episode URL."""
    params = {"url": media_url, "race": str(bool(race)).lower()}
    if server:
        params["server"] = server
    query = urlencode(params)
    data = _get_json(f"{_api_base_url(base_url)}/api/vivarium?{query}")
    return _stream_result(data)


def get_streams(media_id, media_type, season=None, episode=None, server=None,
                race=True, base_url=None):
    """Look up streams using explicit Vivarium/TMDB ID coordinates."""
    params = {
        "id": str(media_id),
        "type": media_type,
        "race": str(bool(race)).lower(),
    }
    if season is not None:
        params["s"] = str(season)
    if episode is not None:
        params["e"] = str(episode)
    if server:
        params["server"] = server
    query = urlencode(params)
    data = _get_json(f"{_api_base_url(base_url)}/api/vivarium?{query}")
    return _stream_result(data)


def _print_search_results(results):
    for index, item in enumerate(results, start=1):
        media_type = item.get("media_type")
        if not media_type:
            media_type = "movie" if "title" in item else "tv"
        title = item.get("title") or item.get("name") or "Untitled"
        year = (item.get("release_date") or item.get("first_air_date") or "")[:4]
        tmdb_id = item.get("id")
        link = (f"https://www.themoviedb.org/{media_type}/{tmdb_id}"
                if tmdb_id else "(no TMDB ID)")
        print(f"{index}. {title} ({year or 'year unknown'}) "
              f"[{media_type}, TMDB {tmdb_id}]")
        print(f"   {link}")


def _print_streams(result):
    streams = result.get("streams") or []
    if not streams:
        print("No streams found.")
        return
    for index, stream in enumerate(streams, start=1):
        print(f"{index}. {stream.get('quality') or 'Auto'}"
              f" | {stream.get('provider') or 'Unknown provider'}"
              f" | {stream.get('type') or 'unknown type'}")
        print(f"   {stream.get('url') or '(missing URL)'}")
        for subtitle in stream.get("subs") or []:
            if isinstance(subtitle, dict) and subtitle.get("url"):
                label = subtitle.get("label") or subtitle.get("lang") or "Subtitle"
                print(f"   Subtitle ({label}): {subtitle['url']}")


def _parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Search TMDB titles and look up streams from Vivarium.")
    parser.add_argument(
        "--api-base-url", default=None,
        help="Vivarium API base URL (default: hosted service or "
             "VIVARIUM_API_BASE_URL)")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser(
        "search", help="Search TMDB for a show or movie title")
    search_parser.add_argument("title")
    search_parser.add_argument("--year", type=int)
    search_parser.add_argument(
        "--type", choices=("multi", "tv", "movie"), default="multi",
        dest="media_type")

    stream_parser = subparsers.add_parser(
        "stream", help="Look up streams from a media URL or explicit ID")
    stream_parser.add_argument(
        "url", nargs="?",
        help="TMDB movie/episode or Vivarium watch URL")
    stream_parser.add_argument("--id", dest="media_id")
    stream_parser.add_argument("--type", choices=("movie", "tv"))
    stream_parser.add_argument("--season", type=int)
    stream_parser.add_argument("--episode", type=int)
    stream_parser.add_argument("--server", choices=("aster", "vexa"))
    stream_parser.add_argument(
        "--all", action="store_true",
        help="Return all sources instead of racing for the first one")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    try:
        if args.command == "search":
            results = search_titles(
                args.title, year=args.year, media_type=args.media_type)
            if not results:
                print("No TMDB matches found.")
            else:
                _print_search_results(results)
            return 0

        if args.url:
            if args.media_id or args.type:
                raise APIError("Use either a URL or --id with --type.")
            result = get_streams_by_url(
                args.url, server=args.server, race=not args.all,
                base_url=args.api_base_url)
        else:
            if not args.media_id or not args.type:
                raise APIError(
                    "Provide a URL, or both --id and --type.")
            result = get_streams(
                args.media_id, args.type, season=args.season,
                episode=args.episode, server=args.server, race=not args.all,
                base_url=args.api_base_url)
        _print_streams(result)
        return 0
    except (APIError, ValueError) as ex:
        print(f"Error: {ex}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
