import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from api import (
    SERVERS,
    anihub_fetch_dual_audio_by_tmdb,
    anihub_fetch_streams_by_tmdb,
    resolve_anihub_course,
    aw_get_servers,
    filter_streams,
    health_vivarium,
    is_dub,
    is_japanese_audio,
    vivarium,
)


class TmdbToAniHubMappingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.response = Mock()
        self.response.status_code = 200
        self.response.json.return_value = {
            "cours": [
                {"al": 159322, "r": [[2, 14, 20], [3, 1, 6]]},
                {"al": 185874, "r": [[3, 7, 19]]},
            ]
        }
        self.client = AsyncMock()
        self.client.get.return_value = self.response

    async def test_race_prefers_vivarium_when_anihub_finishes_first(self):
        vivarium_stream = {
            "url": "https://vivarium.example/actual.m3u8",
            "type": "hls",
            "quality": "1080p English Dub",
            "provider": "Vivarium Source",
        }
        anihub_streams = [{
            "url": "https://anihub.example/embedded.m3u8",
            "type": "hls",
            "quality": "1080p English Dub",
            "provider": "anihub:aniwaves",
        }]
        request = Mock()
        request.app.state.http = self.client

        async def delayed_vivarium(*args, **kwargs):
            await asyncio.sleep(0.01)
            return vivarium_stream

        async def fast_anihub(*args, **kwargs):
            return anihub_streams

        with (
            patch("api.race_streams", side_effect=delayed_vivarium),
            patch("api.anihub_fetch_dual_audio_by_tmdb",
                  side_effect=fast_anihub),
            patch("api._usable_hls_stream", return_value=True),
            patch("api._cache_get", return_value=None),
            patch("api._cache_put"),
        ):
            response = await vivarium(
                request, id="900001", type="tv", url=None, s="1", e="1",
                dub=False, provider=None, server="aster", race=True)

        self.assertEqual(response["status"], 200, response)
        self.assertEqual(
            response["result"]["streams"][0]["provider"], "Vivarium Source")
        self.assertEqual(
            response["result"]["streams"][0]["url"],
            vivarium_stream["url"])

    async def test_returns_labeled_vivarium_profile_fallback(self):
        dub_stream = {
            "url": "https://vivarium.example/dub.m3u8",
            "type": "hls",
            "quality": "1080p Dub",
            "provider": "Iris",
            "subs": [{"url": "https://vivarium.example/en.vtt",
                      "lang": "en", "label": "English"}],
        }
        request = Mock()
        request.app.state.http = self.client

        with (
            patch("api._cache_get", return_value=None),
            patch("api._cached_upstream",
                  new=AsyncMock(return_value={"streams": [dub_stream]})),
            patch("api._cache_put"),
            patch("api.anihub_fetch_dual_audio_by_tmdb",
                  new=AsyncMock(return_value=[])) as anihub_lookup,
        ):
            response = await vivarium(
                request, id="61663", type="tv", url=None, s="1", e="1",
                dub=False, provider=None, server="vexa", race=False)

        self.assertEqual(response["status"], 200, response)
        self.assertEqual(response["result"]["streams"][0]["url"],
                         dub_stream["url"])
        self.assertEqual(response["result"]["streams"][0]["server"], "Aster")
        self.assertEqual(response["result"]["fallback"]["requested_server"],
                         "vexa")
        self.assertEqual(response["result"]["fallback"]["served_server"],
                         "aster")
        anihub_lookup.assert_not_awaited()

    async def test_dual_audio_lookup_scrapes_sub_and_dub_concurrently(self):
        async def fetch_for_audio(tmdb_id, season, episode, audio, client,
                                  first_only=False, resolved_course=None):
            return [{
                "url": f"https://anihub.example/{audio}.m3u8",
                "type": "hls",
                "audio": audio,
                "provider": "aniwaves",
            }]

        with (
            patch("api.resolve_anihub_course",
                  new=AsyncMock(return_value=(20665, 1))),
            patch("api.anihub_fetch_streams_by_tmdb",
                  side_effect=fetch_for_audio) as fetch,
        ):
            streams = await anihub_fetch_dual_audio_by_tmdb(
                61663, 1, 1, self.client, first_only=True)

        self.assertEqual({stream["audio"] for stream in streams},
                         {"sub", "dub"})
        self.assertEqual(fetch.await_count, 2)

    async def test_requested_server_only_scrapes_its_audio_profile(self):
        async def fetch_for_audio(tmdb_id, season, episode, audio, client,
                                  first_only=False, resolved_course=None):
            return [{
                "url": f"https://anihub.example/{audio}.m3u8",
                "type": "hls",
                "audio": audio,
                "provider": "aniwaves",
            }]

        with (
            patch("api.resolve_anihub_course",
                  new=AsyncMock(return_value=(20665, 1))),
            patch("api.anihub_fetch_streams_by_tmdb",
                  side_effect=fetch_for_audio) as fetch,
        ):
            streams = await anihub_fetch_dual_audio_by_tmdb(
                61663, 1, 1, self.client, first_only=True,
                requested_audio="dub")

        self.assertEqual([stream["audio"] for stream in streams], ["dub"])
        fetch.assert_awaited_once()

    async def test_fast_mapping_uses_tmdb_episode_tvdb_identity(self):
        def response(payload):
            result = Mock()
            result.status_code = 200
            result.json.return_value = payload
            return result

        self.client.get.side_effect = [
            response({"mode": "tmdb", "cours": []}),
            response({
                "mappings": {"anilist_id": 20665, "mal_id": 23273},
                "titles": {"en": "Your Lie in April"},
                "episodes": {
                    "1": {"tvdbId": 5001},
                    "3": {"tvdbId": 5004},
                },
            }),
            response({"tvdb_id": 5004}),
        ]

        with (
            patch.dict("os.environ", {
                "TMDB_API_READ_ACCESS_TOKEN": "test-token",
            }),
            patch("api._cache_get", return_value=None),
            patch("api._cache_put"),
            patch("api._cache_anihub") as cache_metadata,
        ):
            course, episode = await resolve_anihub_course(
                61663, 1, 4, self.client)

        self.assertEqual((course, episode), (20665, 3))
        self.assertEqual(self.client.get.await_count, 3)
        self.client.post.assert_not_awaited()
        cache_metadata.assert_called_once()

    async def test_maps_tmdb_episode_with_anizip_metadata(self):
        def response(payload):
            result = Mock()
            result.status_code = 200
            result.json.return_value = payload
            return result

        self.client.get.side_effect = [
            response({"mode": "tmdb", "cours": []}),
            response({"status": "unmapped"}),
            response({"status": "unmapped"}),
            response({
                "name": "Your Lie in April",
                "first_air_date": "2014-10-10",
                "number_of_seasons": 1,
            }),
            response({"air_date": "2014-10-10", "episodes": [
                {
                    "episode_number": number,
                    "name": "Departure" if number == 4 else f"Episode {number}",
                    "air_date": (
                        "2014-10-31" if number == 4
                        else f"2014-10-{number:02d}"),
                }
                for number in range(1, 23)
            ]}),
            response({
                "episodes": {
                    "1": {
                        "airDate": "2014-10-10",
                        "title": {"en": "Monotone / Colorful"},
                    },
                    "2": {"airDate": "2014-10-17",
                          "title": {"en": "Friend A"}},
                    "3": {"airDate": "2014-10-31",
                          "tvdbId": 999,
                          "title": {"en": "Departure"}},
                },
            }),
        ]
        self.client.post.return_value = response({
            "data": {"Page": {"media": [{
                "id": 20665,
                "idMal": 23273,
                "episodes": 22,
                "seasonYear": 2014,
                "title": {
                    "english": "Your Lie in April",
                    "romaji": "Shigatsu wa Kimi no Uso",
                    "native": "四月は君の嘘",
                },
            }]}}
        })
        with (
            patch.dict("os.environ", {
                "TMDB_API_READ_ACCESS_TOKEN": "test-token",
            }),
            patch("api._cache_get", return_value=None),
            patch("api._cache_put"),
            patch("api._cache_anihub"),
        ):
            course, episode = await resolve_anihub_course(
                61663, 1, 4, self.client)

        self.assertEqual((course, episode), (20665, 3))
        self.assertEqual(self.client.get.await_count, 6)
        self.client.post.assert_awaited_once()

    async def test_maps_later_season_to_ani_list_episode_number(self):
        def response(payload):
            result = Mock()
            result.status_code = 200
            result.json.return_value = payload
            return result

        self.client.get.side_effect = [
            response({"mode": "tmdb", "cours": []}),
            response({"status": "unmapped"}),
            response({"status": "unmapped"}),
            response({
                "name": "One-Punch Man",
                "original_name": "ワンパンマン",
            }),
            response({
                "air_date": "2019-04-02",
                "episodes": [
                    {
                        "episode_number": number,
                        "name": (
                            "Return of the Hero" if number == 7
                            else f"Episode {number}"),
                        "air_date": f"2019-04-{number + 1:02d}",
                    }
                    for number in range(1, 13)
                ],
            }),
            response({
                "episodes": {
                    "5": {
                        "airDate": "2019-05-07",
                        "tvdbId": 123,
                        "title": {"en": "Return of the Hero"},
                    },
                },
            }),
        ]
        self.client.post.return_value = response({
            "data": {"Page": {"media": [{
                "id": 97668,
                "idMal": 34134,
                "episodes": 24,
                "seasonYear": 2019,
                "title": {
                    "english": "One-Punch Man 2",
                    "romaji": "One Punch Man 2",
                    "native": "ワンパンマン",
                },
            }]}}
        })
        with (
            patch.dict("os.environ", {
                "TMDB_API_READ_ACCESS_TOKEN": "test-token",
            }),
            patch("api._cache_get", return_value=None),
            patch("api._cache_put"),
            patch("api._cache_anihub"),
        ):
            course, episode = await resolve_anihub_course(
                63926, 2, 7, self.client)

        self.assertEqual((course, episode), (97668, 5))
        self.assertEqual(
            self.client.get.await_args_list[4].args[0],
            "https://api.themoviedb.org/3/tv/63926/season/2")

    async def test_course_resolver_rejects_episodes_outside_configured_ranges(self):
        self.response.json.return_value = {"cours": [
            {"al": 185874, "r": [[3, 7, 19]]},
        ]}
        course, episode = await resolve_anihub_course(
            30984, 3, 7, self.client)

        self.assertEqual(course, 185874)
        self.assertEqual(episode, 1)

    async def test_anihub_fallback_selects_requested_audio_profile(self):
        anihub_streams = [
            {
                "url": "https://anihub.example/sub.m3u8",
                "type": "hls",
                "audio": "sub",
                "provider": "aniwaves",
            },
            {
                "url": "https://anihub.example/dub.m3u8",
                "type": "hls",
                "audio": "dub",
                "provider": "aniwaves",
            },
        ]
        request = Mock()
        request.app.state.http = self.client

        with (
            patch("api._cache_get", return_value=None),
            patch("api._cached_upstream",
                  new=AsyncMock(return_value={"streams": []})),
            patch("api._cache_put"),
            patch("api.anihub_fetch_dual_audio_by_tmdb",
                  new=AsyncMock(return_value=anihub_streams)),
        ):
            response = await vivarium(
                request, id="61663", type="tv", url=None, s="1", e="1",
                dub=False, provider=None, server="vexa", race=False)

        self.assertEqual(response["status"], 200, response)
        self.assertEqual(len(response["result"]["streams"]), 1)
        self.assertEqual(response["result"]["streams"][0]["audio"], "sub")
        self.assertEqual(response["result"]["streams"][0]["server"], "Vexa")

    async def test_anihub_fallback_survives_vivarium_signing_failure(self):
        anihub_streams = [{
            "url": "https://anihub.example/sub.m3u8",
            "type": "hls",
            "audio": "sub",
            "provider": "aniwaves",
        }]
        request = Mock()
        request.app.state.http = self.client

        with (
            patch("api._cache_get", return_value=None),
            patch("api._cached_upstream",
                  new=AsyncMock(side_effect=RuntimeError(
                      "Vivarium request signing failed"))),
            patch("api.anihub_fetch_dual_audio_by_tmdb",
                  new=AsyncMock(return_value=anihub_streams)),
        ):
            response = await vivarium(
                request, id="61663", type="tv", url=None, s="1", e="1",
                dub=False, provider=None, server="vexa", race=False)

        self.assertEqual(response["status"], 200)
        self.assertEqual(response["result"]["streams"][0]["server"], "Vexa")

    async def test_signing_failure_returns_gateway_error_without_fallback(self):
        request = Mock()
        request.app.state.http = self.client

        with (
            patch("api._cache_get", return_value=None),
            patch("api._cached_upstream",
                  new=AsyncMock(side_effect=RuntimeError(
                      "Vivarium request signing failed"))),
            patch("api.anihub_fetch_dual_audio_by_tmdb",
                  new=AsyncMock(return_value=[])),
        ):
            response = await vivarium(
                request, id="61663", type="tv", url=None, s="1", e="1",
                dub=False, provider=None, server="vexa", race=False)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.body, (
            b'{"status":502,"result":"","error":"Vivarium upstream lookup failed",'
            b'"code":"upstream_unavailable",'
            b'"hint":"Vivarium request signing failed"}'))

    async def test_maps_tmdb_position_to_course_local_episode(self):
        course, episode = await resolve_anihub_course(
            30984, 2, 16, self.client)

        self.assertEqual((course, episode), (159322, 3))

    async def test_maps_another_course_for_same_tmdb_series(self):
        course, episode = await resolve_anihub_course(
            30984, 3, 7, self.client)

        self.assertEqual((course, episode), (185874, 1))

    async def test_counts_course_episode_across_tmdb_season_ranges(self):
        course, episode = await resolve_anihub_course(
            30984, 3, 1, self.client)

        self.assertEqual((course, episode), (159322, 8))

    async def test_anihub_scraper_receives_resolved_course_episode(self):
        episode = {
            "number": 3, "source_number": "3",
            "has_sub": True, "has_dub": False,
        }
        with (
            patch("api._get_anihub_mapping",
                  return_value={"title": "Bleach course", "mal_id": 1}),
            patch("api.aw_search", return_value=[
                {"site_id": 1, "slug": "bleach-course"}]),
            patch("api.aw_get_episodes", return_value=[episode]) as get_episodes,
            patch("api.aw_get_servers", return_value=[
                {"link_id": "source-1", "server_name": "Example"}]),
            patch("api.aw_get_sources", return_value={
                "url": "https://stream.example/playlist.m3u8"}),
            patch("api.ak_resolve", return_value=None),
            patch("api.hv_get_episodes", return_value=[]),
        ):
            streams = await anihub_fetch_streams_by_tmdb(
                30984, 2, 16, "sub", self.client)

        self.assertEqual(len(streams), 1)
        get_episodes.assert_called_once_with(
            159322, 1, "bleach-course")

    def test_only_aster_and_vexa_are_exposed(self):
        self.assertEqual([server["server"] for server in SERVERS],
                         ["Aster", "Vexa"])
        self.assertTrue(all(server["status"] == "configured"
                            for server in SERVERS))
        self.assertTrue(all(server["availability"] == "per_title"
                            for server in SERVERS))

    async def test_empty_upstream_provider_list_does_not_mark_profiles_dead(self):
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"providers": []}
        request = Mock()
        request.app.state.http.get = AsyncMock(return_value=response)

        result = await health_vivarium(request)

        self.assertEqual(result["status"], 200)
        self.assertEqual(result["result"]["upstream_status"], "reachable")
        self.assertEqual(
            [profile["status"] for profile in result["result"]["profiles"]],
            ["configured", "configured"])

    def test_server_audio_profiles(self):
        self.assertTrue(is_dub({"audio": "dub"}))
        self.assertFalse(is_dub({"audio_language": "Japanese"}))
        self.assertTrue(is_japanese_audio({"audio_language": "Japanese"}))
        self.assertFalse(is_japanese_audio({"audio_language": "English"}))

    def test_aniwaves_servers_are_scoped_to_the_requested_audio_section(self):
        html = (
            '<div data-type="sub"><ul>'
            '<li data-link-id="sub-id">Sub Server</li>'
            '</ul></div>'
            '<div data-type="dub"><ul>'
            '<li data-link-id="dub-id">Dub Server</li>'
            '</ul></div>'
        )
        with patch("api.anihub_fetch_json",
                   return_value={"result": html}):
            sub_servers = aw_get_servers(1, "1", "sub")
            dub_servers = aw_get_servers(1, "1", "dub")

        self.assertEqual([server["link_id"] for server in sub_servers],
                         ["sub-id"])
        self.assertEqual([server["link_id"] for server in dub_servers],
                         ["dub-id"])

    def test_stream_filter_returns_only_the_two_foreground_servers(self):
        result = filter_streams({
            "streams": [
                {"type": "hls", "quality": "1080p English Dub"},
                {
                    "type": "hls",
                    "quality": "1080p",
                    "audio_language": "Japanese",
                    "subs": [{"lang": "English"}],
                },
                {"type": "hls", "quality": "1080p"},
            ]
        })

        self.assertEqual({stream["server"] for stream in result["streams"]},
                         {"Aster", "Vexa"})


if __name__ == "__main__":
    unittest.main()
