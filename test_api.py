import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

from api import (
    SERVERS,
    anihub_fetch_streams_by_tmdb,
    aw_get_servers,
    filter_streams,
    health_vivarium,
    is_dub,
    is_japanese_audio,
    resolve_anihub_course,
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
            patch("api.anihub_fetch_streams_by_tmdb",
                  side_effect=fast_anihub),
            patch("api._usable_hls_stream", return_value=True),
            patch("api._cache_get", return_value=None),
            patch("api._cache_put"),
        ):
            response = await vivarium(
                request, id="900001", type="tv", s="1", e="1",
                server="aster", race=True)

        self.assertEqual(response["status"], 200)
        self.assertEqual(
            response["result"]["streams"][0]["provider"], "Vivarium Source")
        self.assertEqual(
            response["result"]["streams"][0]["url"],
            vivarium_stream["url"])

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
