import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import series_trailer_downloader as app  # noqa: E402


class SeriesFolderTests(unittest.TestCase):
    def test_parses_year_and_provider_id(self):
        series = app.parse_series_folder(Path(r"C:\TV Shows\The Expanse (2015) [tmdbid-63639]"))
        self.assertEqual(series.display_name, "The Expanse (2015) [tmdbid-63639]")
        self.assertEqual(series.title, "The Expanse")
        self.assertEqual(series.year, "2015")

    def test_cleans_order_prefix_and_provider_id(self):
        self.assertEqual(app.clean_series_title("[042] Severance [tvdbid-371980]"), "Severance")
        self.assertEqual(app.clean_series_title("Severance {tmdb-95396}"), "Severance")

    def test_uses_existing_trailers_folder_case(self):
        with tempfile.TemporaryDirectory() as temp:
            show_path = Path(temp) / "Dark (2017)"
            trailers_path = show_path / "trailers"
            trailers_path.mkdir(parents=True)
            series = app.parse_series_folder(show_path)
            self.assertEqual(app.trailer_directory(series), trailers_path)

    def test_finds_show_trailer_but_not_unrelated_root_video(self):
        with tempfile.TemporaryDirectory() as temp:
            show_path = Path(temp) / "Dark (2017)"
            trailers_path = show_path / "Trailers"
            trailers_path.mkdir(parents=True)
            (trailers_path / "Official Trailer.mp4").write_bytes(b"test")
            (show_path / "unrelated-featurette.mp4").write_bytes(b"test")
            series = app.parse_series_folder(show_path)
            self.assertEqual(app.find_current_trailers(series), [trailers_path / "Official Trailer.mp4"])

    def test_redownload_backup_can_be_restored(self):
        with tempfile.TemporaryDirectory() as temp:
            show_path = Path(temp) / "Dark (2017)"
            trailers_path = show_path / "Trailers"
            trailers_path.mkdir(parents=True)
            original = trailers_path / "Official Trailer.mp4"
            original.write_bytes(b"old trailer")
            series = app.parse_series_folder(show_path)

            renamed = app.rename_existing_trailers(series, dry_run=False)
            self.assertFalse(original.exists())
            self.assertTrue(renamed[0][1].exists())

            restored = app.restore_renamed_trailers(renamed)
            self.assertEqual(restored, [(renamed[0][1], original)])
            self.assertEqual(original.read_bytes(), b"old trailer")

    def test_cli_scan_skips_existing_trailer_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp) / "TV Shows"
            trailers_path = library / "Dark (2017)" / "Trailers"
            trailers_path.mkdir(parents=True)
            (trailers_path / "trailer.mp4").write_bytes(b"existing")
            results_path = Path(temp) / "results.json"
            args = app.build_arg_parser().parse_args(
                ["--root", str(library), "--results-file", str(results_path), "--log-file", ""]
            )

            self.assertEqual(app.run_cli_inner(args), 0)
            results = app.load_results(results_path)
            self.assertEqual(len(results["series"]), 1)

    def test_recursive_scan_finds_shows_but_not_seasons(self):
        with tempfile.TemporaryDirectory() as temp:
            library = Path(temp) / "TV Shows"
            season = library / "D" / "Dark (2017)" / "Season 01"
            season.mkdir(parents=True)
            (season / "Dark S01E01.mkv").write_bytes(b"episode")

            found = list(app.iter_series_folders(library, recursive=True))
            self.assertEqual([item.display_name for item in found], ["Dark (2017)"])


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.series = app.SeriesFolder(Path("The Expanse (2015)"), "The Expanse (2015)", "The Expanse", "2015")

    def test_tmdb_uses_tv_search_and_video_endpoints(self):
        calls = []

        def fake_json(url, headers=None, timeout=15):
            calls.append(url)
            if "/search/tv?" in url:
                return {"results": [{"id": 63639, "name": "The Expanse", "first_air_date": "2015-12-14"}]}
            if "/tv/63639/videos?" in url:
                return {
                    "results": [
                        {
                            "key": "abc123",
                            "name": "Official Series Trailer",
                            "official": True,
                            "site": "YouTube",
                            "size": 1080,
                            "type": "Trailer",
                        }
                    ]
                }
            return None

        with patch.object(app, "http_get_json", side_effect=fake_json):
            candidates, tmdb_id = app.collect_tmdb_candidates(self.series, 600, "test-key", 1080)

        self.assertEqual(tmdb_id, 63639)
        self.assertEqual(len(candidates), 1)
        self.assertTrue(any("/search/tv?" in url and "first_air_date_year=2015" in url for url in calls))
        self.assertTrue(any("/tv/63639/videos?" in url for url in calls))

    def test_rejects_episode_promos_and_wrong_year(self):
        self.assertIsNone(
            app.score_candidate(
                self.series,
                {"title": "The Expanse S01E01 official trailer", "channel": "Prime Video", "duration": 90},
                600,
            )
        )
        self.assertIsNone(
            app.score_candidate(
                self.series,
                {"title": "The Expanse (2024) official trailer", "channel": "Prime Video", "duration": 120},
                600,
            )
        )

    def test_prefers_first_season_over_later_season(self):
        first = app.score_candidate(
            self.series,
            {"title": "The Expanse Season 1 official trailer", "channel": "Prime Video", "duration": 120},
            600,
        )
        later = app.score_candidate(
            self.series,
            {"title": "The Expanse Season 4 official trailer", "channel": "Prime Video", "duration": 120},
            600,
        )
        self.assertIsNotNone(first)
        self.assertIsNotNone(later)
        self.assertGreater(first, later)

    def test_default_sources_are_series_capable(self):
        self.assertEqual(
            app.source_order_from_value(None),
            ["tmdb", "kinocheck", "youtube-api", "internet-archive", "youtube"],
        )


if __name__ == "__main__":
    unittest.main()
