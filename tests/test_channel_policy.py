import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import channel_audit


class ChannelPolicyTests(unittest.TestCase):
    def test_runtime_uses_ten_percent_tolerance(self):
        self.assertTrue(app._youtube_runtime_matches(158, 174))
        self.assertTrue(app._youtube_runtime_matches(157, 174))
        self.assertFalse(app._youtube_runtime_matches(156, 174))
        self.assertFalse(app._youtube_runtime_matches(120, 0))

    def test_omdb_status_requires_runtime_configuration(self):
        with patch.object(app, "OMDB_API_KEY", ""):
            self.assertEqual(app._omdb_status({}), "not_configured")
        with patch.object(app, "OMDB_API_KEY", "omdb-key"):
            self.assertEqual(app._omdb_status({}), "unavailable")
            self.assertEqual(app._omdb_status({"Runtime": "N/A"}), "runtime_missing")
            self.assertEqual(app._omdb_status({"Runtime": "174 min"}), "ready")

    def test_bachelor_runtime_verified_youtube_result(self):
        class FakeResponse:
            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

            def get(self, url, params):
                if url == app.YOUTUBE_SEARCH_URL:
                    return FakeResponse({
                        "items": [{
                            "id": {"videoId": "LD08s4ONvJI"},
                            "snippet": {
                                "title": "Bachelor | Full Movie | GV Prakash Kumar",
                                "channelTitle": "Sony VIZHA",
                                "channelId": "UC-sony",
                                "thumbnails": {},
                            },
                        }]
                    })
                if url == app.YOUTUBE_VIDEOS_URL:
                    return FakeResponse({
                        "items": [{
                            "id": "LD08s4ONvJI",
                            "snippet": {
                                "title": "Bachelor | Full Movie | GV Prakash Kumar",
                                "channelTitle": "Sony VIZHA",
                                "channelId": "UC-sony",
                                "defaultAudioLanguage": "ta",
                                "description": "",
                            },
                            "contentDetails": {"duration": "PT2H38M"},
                            "status": {
                                "privacyStatus": "public",
                                "embeddable": True,
                                "uploadStatus": "processed",
                            },
                        }]
                    })
                raise AssertionError(f"Unexpected URL: {url}")

        app.youtube_full_movie.clear()
        with (
            patch.object(app, "YOUTUBE_API_KEY", "youtube-key"),
            patch.object(app.httpx, "Client", FakeClient),
        ):
            verified = app.youtube_full_movie(
                "Bachelor", "2021", "movie", "ta", 174, True, "bachelor-test"
            )
            unverified = app.youtube_full_movie(
                "Bachelor", "2021", "movie", "ta", 0, True, "bachelor-test-missing"
            )
        self.assertEqual(len(verified), 1)
        self.assertEqual(verified[0]["video_id"], "LD08s4ONvJI")
        self.assertEqual(unverified, [])

    def test_channel_resolution_requires_seed_token_overlap(self):
        candidate = {"snippet": {"title": "Unrelated Movie Channel"}}
        self.assertEqual(channel_audit._candidate_score(candidate, "Zee Tamil"), 0)
        self.assertEqual(
            channel_audit._candidate_score(
                {"snippet": {"title": "Zee Tamil Official"}},
                "Zee Tamil",
            ),
            50,
        )

    def test_movie_title_requires_literal_segment(self):
        title = "Bachelor | Full Movie | GV Prakash Kumar, Divyabharathi"
        self.assertTrue(app._yt_title_exact_match(title, "Bachelor"))
        self.assertFalse(app._yt_title_exact_match("Bachelor Aarambam Full Movie", "Bachelor"))
        self.assertEqual(channel_audit._title_candidates(title)[0], "bachelor")

    def test_curated_seeds_include_requested_channels(self):
        names = {entry["name"] for entry in channel_audit._seed_entries()}
        self.assertIn("Zee Tamil", names)
        self.assertIn("Sony VIZHA", names)
        self.assertTrue(all(entry["tier"] in {"tv", "studio", "movie_official"} for entry in channel_audit._seed_entries()))

    def test_report_is_enforceable_only_when_all_channels_are_reviewed(self):
        qualified = {
            "UC1": {
                "status": "qualified",
                "reviewed": True,
                "official_source": "official-source",
            }
        }
        self.assertTrue(channel_audit._report_is_enforceable(qualified, [], [], set()))
        self.assertFalse(channel_audit._report_is_enforceable({"UC1": {"status": "no_match", "reviewed": True, "official_source": "x"}}, [], [], set()))
        self.assertFalse(channel_audit._report_is_enforceable({"UC1": {"status": "qualified"}}, [], [], set()))
        self.assertFalse(channel_audit._report_is_enforceable(qualified, [{"name": "Zee Tamil"}], [], set()))
        self.assertTrue(channel_audit._report_is_enforceable(qualified, [{"name": "Zee Tamil"}], [], {"zee tamil"}))
        self.assertFalse(channel_audit._report_is_enforceable(qualified, [], ["quota error"], set()))
        self.assertFalse(channel_audit._report_is_enforceable({}, [], [], set()))

    def test_merge_keeps_stricter_movie_tier(self):
        existing = {
            "tier": "tv",
            "status": "qualified",
            "seed_names": ["Studio"],
            "qualifying_videos": [],
            "reviewed": True,
            "official_source": "https://example.test/tv",
        }
        record = {
            "tier": "movie_official",
            "status": "no_match",
            "seed_names": ["Movie"],
            "qualifying_videos": [],
            "reviewed": False,
            "official_source": "",
        }
        merged = channel_audit._merge_record(existing, record)
        self.assertEqual(merged["tier"], "movie_official")
        self.assertEqual(merged["status"], "no_match")
        self.assertEqual(merged["content_check"], "movie_runtime")
        self.assertTrue(merged["reviewed"])
        self.assertEqual(merged["official_source"], "https://example.test/tv")

    def test_report_validation_accepts_reviewed_enforced_report(self):
        report = {
            "schema_version": 1,
            "enforced": True,
            "review_required": False,
            "generated_at": "2026-01-01T00:00:00+00:00",
            "policy": {
                "literal_title_match": True,
                "runtime_tolerance": 0.1,
            },
            "channels": {
                "UC123456789": {
                    "channel_id": "UC123456789",
                    "tier": "movie_official",
                    "status": "qualified",
                    "reviewed": True,
                    "official_source": "https://example.test/source",
                }
            },
            "unresolved": [],
            "errors": [],
            "waived": [],
        }
        self.assertTrue(app._channel_audit_is_valid(report))

    def test_report_validation_rejects_weaker_policy(self):
        report = {
            "schema_version": 1,
            "enforced": True,
            "policy": {
                "literal_title_match": False,
                "runtime_tolerance": 0.1,
            },
            "channels": {
                "UC1": {
                    "status": "qualified",
                    "reviewed": True,
                    "official_source": "https://example.test",
                }
            },
        }
        self.assertFalse(app._channel_audit_is_valid(report))

    def test_request_errors_do_not_include_api_key(self):
        class FakeClient:
            def get(self, url, params):
                request = httpx.Request("GET", "https://youtube.test")
                return httpx.Response(429, request=request)

        with patch.object(app, "YOUTUBE_API_KEY", "secret-key"):
            with self.assertRaises(channel_audit.AuditError) as context:
                channel_audit._youtube_request(FakeClient(), "https://youtube.test", {})
        self.assertNotIn("secret-key", str(context.exception))
        self.assertIn("429", str(context.exception))

    def test_audit_stops_after_quota_error(self):
        seed = {
            "name": "Example TV",
            "tier": "tv",
            "languages": ["ta"],
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "channel_audit.json"
            with (
                patch.object(app, "YOUTUBE_API_KEY", "youtube-key"),
                patch.object(channel_audit, "_seed_entries", return_value=[seed]),
                patch.object(
                    channel_audit,
                    "_resolve_channel",
                    side_effect=channel_audit.AuditQuotaReached("HTTP 429"),
                ),
            ):
                report = channel_audit.audit(output, max_pages=1, max_omdb_lookups=1)
            self.assertTrue(output.exists())
            self.assertTrue(report["quota_exhausted"])
            self.assertEqual(len(report["errors"]), 1)

    def test_identity_channel_qualifies_after_one_upload_page(self):
        def fake_batches(client, playlist, max_pages, state):
            state["pages"] = 1
            yield ["video-1"]

        metadata = {
            "channel_id": "UC-tv",
            "title": "Example TV",
            "uploads_playlist": "UU-tv",
        }
        seed = {
            "name": "Example TV",
            "tier": "tv",
            "languages": ["ta"],
        }
        with patch.object(channel_audit, "_upload_batches", fake_batches):
            record = channel_audit._audit_channel(
                object(),
                seed,
                metadata,
                max_pages=20,
                max_omdb_lookups=0,
                max_matches=1,
                omdb_cache={},
                omdb_state={"lookups": 0},
            )
        self.assertEqual(record["status"], "qualified")
        self.assertTrue(record["scan_complete"])
        self.assertEqual(record["pages"], 1)

    def test_video_audit_requires_omdb_runtime_and_accepts_ten_percent_match(self):
        video = {
            "id": "LD08s4ONvJI",
            "snippet": {
                "channelId": "UC-sony",
                "channelTitle": "Sony VIZHA",
                "defaultAudioLanguage": "ta",
                "title": "Bachelor | Full Movie | GV Prakash Kumar",
                "description": "",
            },
            "contentDetails": {"duration": "PT2H38M"},
            "status": {
                "privacyStatus": "public",
                "embeddable": True,
                "uploadStatus": "processed",
            },
        }
        seed = {"channel_id": "UC-sony", "languages": ["ta"]}
        details = {
            "Title": "Bachelor",
            "Year": "2021",
            "Runtime": "174 min",
            "imdbID": "tt11396290",
        }
        with patch.object(channel_audit, "_omdb_request", return_value=details):
            match = channel_audit._video_record(
                object(), video, seed, {}, {"lookups": 0}, 0
            )
        self.assertIsNotNone(match)
        self.assertEqual(match["omdb_minutes"], 174)
        with patch.object(channel_audit, "_omdb_request", return_value={**details, "Runtime": "N/A"}):
            missing_runtime = channel_audit._video_record(
                object(), video, seed, {}, {"lookups": 0}, 0
            )
        self.assertIsNone(missing_runtime)

    def test_enforced_audit_requires_exact_channel_id(self):
        report = {
            "UC-audited": {"status": "qualified", "tier": "movie_official"},
        }
        with (
            patch.object(app, "_CHANNEL_AUDIT_ENFORCED", True),
            patch.object(app, "_CHANNEL_AUDIT_CHANNELS", report),
        ):
            self.assertEqual(
                app._channel_tier_for_video("Sony VIZHA", "UC-audited"),
                "movie_official",
            )
            self.assertEqual(app._channel_tier_for_video("Sony VIZHA", "UC-other"), "")


if __name__ == "__main__":
    unittest.main()
