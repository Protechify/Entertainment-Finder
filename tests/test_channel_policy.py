import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

import app
import channel_audit


class ChannelPolicyTests(unittest.TestCase):
    def setUp(self):
        # `search` is cached, so a result built under one set of patched
        # providers would otherwise be handed to a later test expecting a
        # different answer.
        app.search.clear()

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

    def test_omdb_status_separates_config_from_quota_and_network(self):
        """A missing key, a rejected key, an exhausted quota and a dead network
        are four different problems, so they must not collapse into one
        'set OMDB_API_KEY' message."""
        with patch.object(app, "OMDB_API_KEY", "omdb-key"):
            self.assertEqual(app._omdb_status({}, ""), "unavailable")
            self.assertEqual(app._omdb_status({}, "network error"), "unavailable")
            self.assertEqual(
                app._omdb_status({}, "Daily limit exceeded!"), "rate_limited"
            )
            self.assertEqual(
                app._omdb_status({}, "Daily limit exceeded (1000 per day)"),
                "rate_limited",
            )
            self.assertEqual(app._omdb_status({}, "Invalid API key!"), "auth_failed")
            self.assertEqual(app._omdb_status({}, "Movie not found!"), "not_found")
        # A key that is present but rejected is still "not configured" as far as
        # the user is concerned, so the message points at the same field.
        with patch.object(app, "OMDB_API_KEY", ""):
            self.assertEqual(app._omdb_status({}, "Invalid API key!"), "not_configured")

    def test_rate_limit_message_names_the_limit_not_the_key(self):
        message = app._omdb_status_message("rate_limited")
        self.assertIn("1,000 requests/day", message)
        self.assertNotIn("OMDB_API_KEY", message)
        self.assertIn("OMDB_API_KEY", app._omdb_status_message("not_configured"))
        for status in (
            "not_configured", "auth_failed", "rate_limited", "not_found",
            "unavailable", "runtime_missing",
        ):
            self.assertTrue(app._omdb_status_message(status))
        self.assertTrue(app._omdb_status_message("some_future_state"))

    def test_omdb_fetch_surfaces_the_api_error_instead_of_discarding_it(self):
        class FakeResponse:
            status_code = 401

            def __init__(self, payload):
                self.payload = payload

            def json(self):
                return self.payload

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params):
                return FakeResponse(
                    {"Response": "False", "Error": "Daily limit exceeded!"}
                )

        with patch.object(app.httpx, "Client", FakeClient):
            details, error = app._omdb_fetch({"i": "tt0816692"})
        self.assertEqual(details, {})
        self.assertEqual(error, "Daily limit exceeded!")

    def test_omdb_fetch_reports_transport_failures(self):
        class FailingClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params):
                raise app.httpx.ConnectError("no route to host")

        class BadJsonClient(FailingClient):
            def get(self, url, params):
                class Resp:
                    def json(self):
                        raise ValueError("not json")

                return Resp()

        with patch.object(app.httpx, "Client", FailingClient):
            self.assertEqual(app._omdb_fetch({"i": "x"}), ({}, "network error"))
        with patch.object(app.httpx, "Client", BadJsonClient):
            self.assertEqual(app._omdb_fetch({"i": "x"}), ({}, "invalid response"))

    def test_omdb_details_returns_details_with_the_reason(self):
        class FakeResponse:
            status_code = 200

            def json(self):
                return {"Response": "True", "Runtime": "169 min", "imdbID": "tt0816692"}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params):
                return FakeResponse()

        selected = {
            "id": "tt0816692",
            "media_type": "movie",
            "title": "Interstellar",
            "year": "2014",
        }
        with patch.object(app.httpx, "Client", FakeClient):
            details, error = app.omdb_details(selected)
        self.assertEqual(error, "")
        self.assertEqual(app._omdb_status(details, error), "ready")

    def test_dotenv_resolves_regardless_of_working_directory(self):
        """Streamlit is often launched from a different folder, so the .env next
        to app.py must be used instead of one relative to the CWD."""
        self.assertEqual(
            Path(app.DOTENV_PATH).resolve(),
            Path(app.__file__).resolve().with_name(".env"),
        )
        project_env = Path(app.DOTENV_PATH)
        if not project_env.exists():
            self.skipTest("no project .env in this checkout")
        expected = {}
        for line in project_env.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() and "=" in line:
                expected[key.strip()] = value.strip().strip('"').strip("'")

        with tempfile.TemporaryDirectory() as elsewhere:
            (Path(elsewhere) / ".env").write_text("OMDB_API_KEY=wrong-dir\n", encoding="utf-8")
            previous = os.environ.get("OMDB_API_KEY")
            os.environ.pop("OMDB_API_KEY", None)
            origin = os.getcwd()
            try:
                os.chdir(elsewhere)
                app._load_dotenv()
                self.assertEqual(
                    os.environ["OMDB_API_KEY"],
                    expected.get("OMDB_API_KEY", "wrong-dir"),
                )
                if "OMDB_API_KEY" in expected:
                    self.assertNotEqual(os.environ["OMDB_API_KEY"], "wrong-dir")
            finally:
                os.chdir(origin)
                if previous is None:
                    os.environ.pop("OMDB_API_KEY", None)
                else:
                    os.environ["OMDB_API_KEY"] = previous

    def test_empty_environment_variable_does_not_shadow_dotenv(self):
        """`OMDB_API_KEY=` in the shell must not beat the value in .env."""
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OMDB_API_KEY=from-dotenv\n", encoding="utf-8")
            previous = os.environ.get("OMDB_API_KEY")
            os.environ["OMDB_API_KEY"] = ""
            try:
                app._load_dotenv(str(env_path))
                self.assertEqual(os.environ["OMDB_API_KEY"], "from-dotenv")
            finally:
                if previous is None:
                    os.environ.pop("OMDB_API_KEY", None)
                else:
                    os.environ["OMDB_API_KEY"] = previous

    def test_load_dotenv_keeps_a_real_environment_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("OMDB_API_KEY=from-dotenv\n", encoding="utf-8")
            previous = os.environ.get("OMDB_API_KEY")
            os.environ["OMDB_API_KEY"] = "from-environment"
            try:
                app._load_dotenv(str(env_path))
                self.assertEqual(os.environ["OMDB_API_KEY"], "from-environment")
            finally:
                if previous is None:
                    os.environ.pop("OMDB_API_KEY", None)
                else:
                    os.environ["OMDB_API_KEY"] = previous

    def test_tier_badge_marks_broadcaster_tv_channels(self):
        """A TV channel rebroadcast and a studio upload both pass the runtime
        check, so the badge is what tells them apart."""
        self.assertIn("TV Channel", app._tier_badge("tv"))
        self.assertIn("Official Studio", app._tier_badge("studio"))
        self.assertIn("Official Label", app._tier_badge("movie_official"))
        self.assertEqual(app._tier_badge(""), "")
        self.assertEqual(app._tier_badge("untrusted"), "")

    def test_requested_tv_channels_resolve_to_the_tv_tier(self):
        for name in ("Jaya TV", "Kalaingar TV", "Sun TV", "Zee Tamil", "Star Vijay"):
            self.assertEqual(app._channel_tier(name), "tv", name)
        self.assertEqual(app._tier_badge(app._channel_tier("Jaya TV")),
                         app._tier_badge("tv"))

    def test_tier_badge_escapes_untrusted_channel_text(self):
        self.assertNotIn("<script", app._tier_badge("tv"))
        self.assertIn("&lt;img", app._chip('<img src=x onerror=alert(1)>', "#fff"))
        self.assertNotIn('onerror=alert(1)>"', app._chip('<img src=x onerror=alert(1)>', "#fff"))

    def test_youtube_failures_are_reported_separately_from_empty_results(self):
        """An exhausted quota is not the same as 'this film isn't on YouTube'."""
        for status in ("quota", "auth", "unavailable", "error"):
            self.assertIn(status, app._YT_FAILURE_STATES)
            self.assertTrue(app._yt_status_message(status))
        self.assertNotIn("ok", app._YT_FAILURE_STATES)
        self.assertNotIn("no_results", app._YT_FAILURE_STATES)
        self.assertEqual(app._yt_status_message("no_results"), "")
        self.assertIn("quota", app._yt_status_message("quota").casefold())

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
        self.assertTrue(verified[0]["runtime_verified"])
        # No OMDb runtime to compare against: the upload is still listed, but
        # explicitly not claimed as length-verified.
        self.assertEqual(len(unverified), 1)
        self.assertEqual(unverified[0]["video_id"], "LD08s4ONvJI")
        self.assertFalse(unverified[0]["runtime_verified"])

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

    def test_movie_title_ignores_language_and_quality_scaffolding(self):
        """'<Film> Full Movie In English' is the most common upload shape, and
        dropping 'full'/'movie'/'english' but not the 'in' used to leave
        'interstellarin', which matched nothing."""
        for result in (
            "Interstellar Full Movie In English | Hollywood Movie",
            "Interstellar (2014) | Full Movie | Matthew McConaughey",
            "INTERSTELLAR Full Movie in English HD",
            "Vedalam 2015 Hindi Dubbed Movie",
        ):
            self.assertTrue(app._yt_title_exact_match(result, result.split()[0]), result)
        self.assertTrue(
            app._yt_title_exact_match("Interstellar Full Movie In English", "Interstellar")
        )

    def test_movie_title_still_rejects_a_different_film(self):
        """Loosening the scaffolding must not let a similar name through."""
        for result, title in (
            ("Interstellar Wars Full Movie", "Interstellar"),
            ("Dark Knight Returns Full Movie", "The Dark Knight"),
            ("Aanandham | Tamil Full Movie | Jayaram", "Aanandham Aarambam"),
            ("Bigil (Whistle) Telugu Full Movie | Vijay", "Bigil 2"),
        ):
            self.assertFalse(app._yt_title_exact_match(result, title), result)

    def test_movie_title_with_stopwords_still_matches_itself(self):
        """Stripping function words must be symmetric, or a film whose own name
        contains one ('Life in a Nutshell') could never match."""
        for title in ("Life in a Nutshell", "The Dark Knight", "Alien 3"):
            self.assertTrue(
                app._yt_title_exact_match(f"{title} | Full Movie", title), title
            )
        self.assertEqual(
            app._yt_title_core("Life in a Nutshell"), app._yt_title_core("life in a nutshell")
        )
        self.assertEqual(app._yt_title_core(""), "")

    def test_unverified_results_are_labelled_not_hidden(self):
        """Widening the net must not let an unreviewed channel's upload look
        official, so it gets its own badge."""
        unverified = app._trust_badges("")
        self.assertIn("Unverified Channel", unverified)
        self.assertNotIn("TV Channel", unverified)
        self.assertIn("Runtime ✓", unverified)
        self.assertIn("TV Channel", app._trust_badges("tv"))
        self.assertIn("Official Studio", app._trust_badges("studio"))
        self.assertIn("Official Label", app._trust_badges("movie_official"))
        self.assertNotIn("Unverified Channel", app._trust_badges("studio"))

    def test_title_match_survives_romanisation_drift(self):
        """Regional catalogues and uploaders spell the same film differently —
        OMDb/JustWatch write "Samuthiram", the studio's own channel writes
        "Samudhiram". Rejecting that hid a film that is genuinely on YouTube.
        """
        self.assertTrue(app._yt_title_exact_match(
            "Samudhiram | Tamil Full Movie | Sarath Kumar, Abhira", "Samuthiram"
        ))
        # ...while the films that merely open the same way stay rejected.
        for other in (
            "Samasthanam HD Tamil Full Movie | Sarathkumar",
            "Aanandham - Tamil Full Movie | Mammootty",
            "Maayi - Tamil Full Movie | Sarath Kumar",
            "Surya Vamsam - Tamil Full Movie | Sarathkumar",
        ):
            self.assertFalse(
                app._yt_title_exact_match(other, "Samuthiram"), other
            )

    def test_title_match_keeps_numbered_entries_apart(self):
        """A fuzzy title comparison rates "kgfchapter1" and "kgfchapter2" at
        0.92, so a different chapter would pass as the film. Numbers must
        agree, and a one-letter sequel tail must not read as a spelling
        variant ("3 Idiots" vs "3 Idiots K" is 0.93 similar)."""
        self.assertFalse(app._yt_title_exact_match(
            "K.G.F: Chapter 2 Full Movie", "K.G.F: Chapter 1"
        ))
        self.assertFalse(app._yt_title_exact_match(
            "KGF Chapter 2 Full Movie in Hindi | Rocking Star Yash", "K.G.F: Chapter 1"
        ))
        self.assertTrue(app._yt_title_exact_match(
            "Kgf chapter 1 Full movies", "K.G.F: Chapter 1"
        ))
        self.assertFalse(app._yt_title_exact_match("3 Idiots K Full Movie", "3 Idiots"))

    def test_title_match_separates_villan_from_villain(self):
        """An extra letter at the end of a word is a different word, and the
        fuzzy step rates "villan"/"villain" at 0.92. Without this, searching
        the Ajith Kumar film offered up "Mahaabali 2 - The Villain"."""
        self.assertFalse(app._yt_title_exact_match(
            "Mahaabali 2 - The Villain Full Hindi Dubbed Movie", "Villan"
        ))
        self.assertFalse(app._yt_title_exact_match(
            "Villain Tamil Blockbuster Movie | Ajith Kumar, Meena", "Villan"
        ))
        self.assertTrue(app._yt_title_exact_match(
            "Villan (2002) Full Tamil Movie | Ajith Kumar | Meena", "Villan"
        ))
        # A plural suffix is still the same film.
        self.assertTrue(app._yt_title_exact_match("Avengers Full Movie", "Avenger"))
        # A misspelling that inserts a letter is indistinguishable from a
        # different word, so it is not auto-corrected here; the app offers
        # suggestions separately.
        self.assertFalse(app._yt_title_exact_match("Villian Full Movie", "Villan"))

    def test_title_match_does_not_fall_back_to_containment(self):
        """A sequel's title is its predecessor plus extra words, so accepting
        "shorter title is inside longer title" would answer every query for the
        first film with uploads of the second."""
        self.assertFalse(app._yt_title_exact_match(
            "Aanandham Aarambam Tamil Full Movie", "Aanandham"
        ))
        self.assertFalse(app._yt_title_exact_match(
            "Ramayana Full Movie | Ranbir Kapoor", "Ram"
        ))

    def test_plural_markers_are_stripped(self):
        """Titles are inconsistent about plurals. Only explicit entries are
        safe here: stripping a trailing "s" would turn "3 Idiots" into
        "3 Idiot"."""
        self.assertEqual(app._yt_title_core("Kgf chapter 1 Full movies"), "kgfchapter1")
        self.assertEqual(app._yt_title_core("3 Idiots"), "3idiots")
        self.assertTrue(app._yt_title_exact_match(
            "Kgf chapter 1 Full movies", "K.G.F: Chapter 1"
        ))

    def test_search_filter_matches_words_not_glued_characters(self):
        """Glueing a title's characters together let "Villa Negra" become
        "villanegra", which contains "villan" and put an unrelated 1963 film at
        the top of a search for the Ajith Kumar movie."""
        self.assertFalse(app._title_contains_query("villan", "Villa Negra"))
        self.assertFalse(app._title_contains_query("villan", "Villa Nabila"))
        self.assertFalse(app._title_contains_query("villan", "Villanelle"))
        self.assertFalse(app._title_contains_query("villan", "Villanegyed"))
        self.assertTrue(app._title_contains_query("villan", "Villan"))
        self.assertTrue(app._title_contains_query("villan", "The Villan Still Pursued"))
        # A query word sitting inside a title word still counts.
        self.assertTrue(app._title_contains_query("iron man", "Ironman"))
        self.assertTrue(app._title_contains_query("bigil", "Bigil Diwali"))

    def test_year_is_read_as_a_year_not_a_title_word(self):
        """'Villan 2011' was rejected outright because '2011' became a required
        title token, so the search returned nothing."""
        self.assertEqual(app._extract_year("Villan 2011"), ("Villan", "2011"))
        self.assertEqual(app._extract_year("villan"), ("villan", ""))
        self.assertEqual(
            app._extract_year("  3   Idiots   2009 "), ("3 Idiots", "2009")
        )
        self.assertEqual(app._extract_year("Blade Runner 2049"), ("Blade Runner", "2049"))

    def test_unidentifiable_youtube_only_row_still_skips_omdb_title_lookup(self):
        """When the film cannot be identified, the row falls back to reporting no
        record rather than guessing. A `t=` lookup would resolve 'Villan' to a
        1920 silent film and hang the wrong plot and cast under it."""
        row = {
            "id": "", "media_type": "movie", "title": "Villan", "year": "",
            "poster": None, "link": "", "youtube_only": True,
        }
        with patch.object(app, "_imdb_suggest", return_value=[]):
            details, error = app.omdb_details(row)
        self.assertEqual(details, {})
        self.assertIn("catalog", error)
        self.assertEqual(app._omdb_status(details, error), "not_found")

    def test_youtube_only_row_is_offered_only_when_needed(self):
        """A film missing from OMDb and JustWatch gets a YouTube-only row; a
        title the catalogue already has must not gain one."""
        real = [{
            "id": "tt0816692", "media_type": "movie",
            "title": "Interstellar", "year": "2014", "poster": None, "link": "",
        }]
        self.assertTrue(app._has_exact_title(real, "Interstellar"))
        self.assertFalse(app._has_exact_title([], "Interstellar"))
        with patch.object(app, "omdb_search", return_value=real), \
             patch.object(app, "justwatch_search", return_value=[]), \
             patch.object(app, "_variant_pass", side_effect=lambda q, y, m, k: k), \
             patch.object(app, "_offer_youtube_only") as offer:
            found = app.search("interstellar")
        offer.assert_not_called()
        self.assertFalse(any(r.get("youtube_only") for r in found))
        # Nothing in the catalogue: the fallback is consulted.
        with patch.object(app, "omdb_search", return_value=[]), \
             patch.object(app, "justwatch_search", return_value=[]), \
             patch.object(app, "_variant_pass", side_effect=lambda q, y, m, k: k), \
             patch.object(app, "_offer_youtube_only", return_value=[{"youtube_only": True}]) as offer:
            found = app.search("villan")
        offer.assert_called_once()
        self.assertTrue(found[0].get("youtube_only"))


    def test_imdb_suggestions_are_parsed_into_catalogue_rows(self):
        """Only real titles come back: people, episodes and unnamed entries are
        dropped, and each row is shaped like one from omdb_search."""
        payload = {"d": [
            {"id": "tt0417241", "l": "Villain", "y": 2002, "qid": "movie",
             "s": "Ajith Kumar, Neha Dhupia"},
            {"id": "tt0417242", "l": "Villain", "y": 2020, "qid": "movie", "s": ""},
            {"id": "tt0417243", "l": "Villain", "y": 2020, "qid": "series", "s": ""},
            {"id": "nm0898473", "l": "Mayte Vilán", "qid": None, "s": "Actress"},
            {"id": "tt0417244", "l": "", "y": 1999, "qid": "movie", "s": ""},
        ]}
        with patch.object(app, "_imdb_suggest_cached", return_value=[
            {"id": r["id"], "media_type": r["qid"] == "series" and "tv" or "movie",
             "title": r["l"], "year": str(r["y"])[:4], "poster": None,
             "link": f"https://www.imdb.com/title/{r['id']}/"}
            for r in payload["d"] if r["id"].startswith("tt") and r["l"]
        ]):
            rows = app._imdb_suggest("Villan 2002")
        self.assertEqual([r["id"] for r in rows],
                         ["tt0417241", "tt0417242", "tt0417243"])
        self.assertEqual([r["media_type"] for r in rows],
                         ["movie", "movie", "tv"])
        self.assertEqual(rows[0]["year"], "2002")
        self.assertTrue(rows[0]["link"].endswith("tt0417241/"))

    def test_a_suggestion_is_accepted_only_when_the_year_agrees(self):
        """IMDb matches on the name alone, so 'Villain' returns a 1971 film, a
        2020 film and the 2002 one. Only the year tells them apart, and a
        candidate with no year at all must be refused rather than guessed at."""
        def cand(year, title="Villain", kind="movie"):
            return {"id": "tt1", "media_type": kind, "title": title,
                    "year": year, "poster": None, "link": ""}
        # The film that was wanted: right year, one letter from the typo.
        self.assertTrue(app._imdb_candidate_is_the_film(cand("2002"), "Villan", "2002", "movie"))
        # Its namesakes, which the year excludes.
        self.assertFalse(app._imdb_candidate_is_the_film(cand("2020"), "Villan", "2002", "movie"))
        self.assertFalse(app._imdb_candidate_is_the_film(cand("1971"), "Villan", "2002", "movie"))
        # Right year, wrong film entirely.
        self.assertFalse(app._imdb_candidate_is_the_film(cand("2002", "Villa des roses"), "Villan", "2002", "movie"))
        # Right name, wrong medium.
        self.assertFalse(app._imdb_candidate_is_the_film(cand("2002", kind="tv"), "Villan", "2002", "movie"))
        # No year anywhere: refuse rather than attach a plausible wrong film.
        self.assertFalse(app._imdb_candidate_is_the_film(cand(""), "Villan", "", "movie"))
        self.assertFalse(app._imdb_candidate_is_the_film(cand("1971"), "Villan", "", "movie"))

    def test_villan_resolves_to_the_2002_film_and_fills_in_the_row(self):
        """The reported bug: 'Villan' is spelled one letter away from the real
        title, and OMDb's search returns nothing for either spelling. The row
        was left with an empty details panel; it must now resolve, and carry the
        year, poster and IMDb link with it."""
        suggestions = [
            {"id": "tt0417241", "media_type": "movie", "title": "Villain",
             "year": "2002", "poster": None,
             "link": "https://www.imdb.com/title/tt0417241/"},
            {"id": "tt9820352", "media_type": "movie", "title": "Villain",
             "year": "2020", "poster": None,
             "link": "https://www.imdb.com/title/tt9820352/"},
        ]
        row = {"id": "", "media_type": "movie", "title": "Villan", "year": "2002",
               "poster": None, "link": "", "youtube_only": True}
        record = {
            "Response": "True", "Title": "Villain", "Year": "2002", "imdbID": "tt0417241",
            "Runtime": "162 min", "Plot": "Shiva, a bus conductor...",
            "Actors": "Ajith Kumar, Neha Dhupia, Meena", "Director": "K.S. Ravikumar",
            "Poster": "https://example.invalid/p.jpg",
        }
        sent = {}
        def fake_fetch(params):
            sent.update(params)
            return record, ""
        with patch.object(app, "_imdb_suggest", return_value=suggestions), \
             patch.object(app, "_omdb_fetch", side_effect=fake_fetch), \
             patch.object(app, "_poster_ok", return_value=True):
            details, error = app.omdb_details(row)
        self.assertEqual(error, "")
        self.assertEqual(details["imdbID"], "tt0417241")
        self.assertEqual(details["Runtime"], "162 min")
        # Asked by id, never by name: `t=Villan` returns a 1920 silent film.
        self.assertEqual(sent.get("i"), "tt0417241")
        self.assertNotIn("t", sent)
        # The row the UI renders is enriched in place.
        self.assertEqual(row["id"], "tt0417241")
        self.assertEqual(row["title"], "Villain")
        self.assertEqual(row["year"], "2002")
        self.assertEqual(row["poster"], "https://example.invalid/p.jpg")
        self.assertTrue(row["link"].endswith("tt0417241/"))

    def test_a_resolved_year_turns_the_row_into_a_real_catalogue_entry(self):
        """Once the upload's own title reveals the year, the results list can
        offer the real film instead of a placeholder row."""
        suggestions = [
            {"id": "tt0417241", "media_type": "movie", "title": "Villain",
             "year": "2002", "poster": None,
             "link": "https://www.imdb.com/title/tt0417241/"},
        ]
        uploads = [{"title": "Villan (2002) Full Tamil Movie | Ajith Kumar | Sun NXT Free Movies"}]
        with patch.object(app, "youtube_full_movie", return_value=uploads), \
             patch.object(app, "_imdb_suggest", return_value=suggestions):
            rows = app._offer_youtube_only("villan", "", [])
        self.assertEqual(rows[0]["id"], "tt0417241")
        self.assertEqual(rows[0]["title"], "Villain")
        self.assertEqual(rows[0]["year"], "2002")
        self.assertNotIn("youtube_only", rows[0])

    def test_title_variants_cover_the_misspelling_but_stay_short(self):
        """'villan' must reach 'villain', while the list stays small enough that
        a catalogue miss cannot dominate the time a search takes."""
        variants = app._title_variants("villan")
        self.assertIn("villain", variants)
        self.assertNotIn("villan", variants)
        self.assertLessEqual(len(variants), app._TITLE_VARIANT_LIMIT)
        # Too short to have a meaningful misspelling.
        self.assertEqual(app._title_variants("kgf"), [])
        # A real word is not mangled into nonsense.
        for variant in app._title_variants("interstellar"):
            self.assertTrue(variant.isalpha())

    def test_a_misspelling_is_offered_as_a_correction(self):
        """A search for 'villan' returns near misses; the film the user meant
        has to be put in front of them as an explicit correction."""
        results = [
            {"title": "The Villan Still Pursued", "year": "1920", "media_type": "movie"},
            {"title": "Villa Negra", "year": "2021", "media_type": "movie"},
            {"title": "Villain", "year": "2002", "media_type": "movie"},
        ]
        picks = app._correction_candidates("villan", results)
        self.assertEqual([row["title"] for row in picks], ["Villain"])

    def test_an_unrelated_title_is_not_offered_as_a_correction(self):
        """'John Wick' scores well on word overlap, so correction candidates
        have to be chosen by similarity instead or every short query would be
        'corrected' into an unrelated film."""
        results = [
            {"title": "John Wick", "year": "2014", "media_type": "movie"},
            {"title": "Jumanji", "year": "2017", "media_type": "movie"},
        ]
        self.assertEqual(app._correction_candidates("jonas", results), [])

    def test_a_correct_spelling_is_left_alone(self):
        """The right answer is never presented as a correction of itself."""
        results = [{"title": "Interstellar", "year": "2014", "media_type": "movie"}]
        self.assertTrue(app._has_exact_title(results, "interstellar"))
        self.assertEqual(app._correction_candidates("", results), [])

    def test_a_justwatch_id_is_looked_up_by_imdb_id_instead(self):
        """JustWatch ids are tm…/tv… entry numbers OMDb cannot resolve, so its
        rows are searched by name — and a name fails whenever the two catalogues
        disagree on spelling. "Naan Sirithal" and "Naan Sirithaal" are the same
        film, and only one of those spellings is in OMDb."""
        row = {
            "id": "tm852819", "media_type": "movie", "title": "Naan Sirithal",
            "year": "2020", "poster": None, "link": "https://justwatch.com/in/movie/naan-sirithal",
        }
        bridge = {
            "id": "tt11138290", "media_type": "movie", "title": "Naan Sirithaal",
            "year": "2020", "poster": None, "link": "https://www.imdb.com/title/tt11138290/",
        }
        with patch.object(app, "_resolve_by_imdb", return_value=bridge) as bridge_call, \
                patch.object(app, "_omdb_fetch", return_value=({}, "Movie not found!")) as fetch:
            def answer(params):
                return ({"Title": "Naan Sirithaal", "Year": "2020", "Response": True}, "") \
                    if params.get("i") == "tt11138290" else ({}, "Movie not found!")
            fetch.side_effect = answer
            details, error = app.omdb_details(row)

        self.assertEqual(details.get("Title"), "Naan Sirithaal")
        self.assertEqual(error, "")
        bridge_call.assert_called_once()
        # The row is corrected in place so the heading and the link agree with
        # the record that was actually found.
        self.assertEqual(row["id"], "tt11138290")
        self.assertEqual(row["title"], "Naan Sirithaal")
        self.assertIn("imdb.com/title/tt11138290", row["link"])

    def test_a_title_already_holding_an_imdb_id_never_asks_the_bridge(self):
        """The common case must not pay for an extra lookup: an OMDb row already
        carries a tt… id and resolves on the first request."""
        row = {"id": "tt0417241", "media_type": "movie", "title": "Villain", "year": "2002", "poster": None, "link": ""}
        with patch.object(app, "_resolve_by_imdb") as bridge_call, \
                patch.object(app, "_omdb_fetch", return_value=({"Title": "Villain", "Response": True}, "")) as fetch:
            app.omdb_details(row)
        bridge_call.assert_not_called()
        self.assertEqual(fetch.call_args.args[0]["i"], "tt0417241")

    def test_a_genuine_miss_is_reported_without_a_bridged_title(self):
        """When the bridge agrees on nothing, the row keeps its own id and year.
        Inventing an identity here is what would put an unrelated film's plot
        under the title the user searched for."""
        row = {"id": "tm999999", "media_type": "movie", "title": "Zzzqqq", "year": "1999", "poster": None, "link": ""}
        with patch.object(app, "_resolve_by_imdb", return_value=None), \
                patch.object(app, "_omdb_fetch", return_value=({}, "Movie not found!")):
            details, error = app.omdb_details(row)
        self.assertEqual(details, {})
        self.assertEqual(error, "Movie not found!")
        self.assertEqual(row["id"], "tm999999")

    def test_cached_youtube_results_survive_the_quota_latch(self):
        """The latch guards the search, not the cache. A title already looked
        up today is still in the six-hour cache, and reporting it as unchecked
        would state the opposite of what the app knows."""
        self.addCleanup(setattr, app, "_YT_QUOTA_SPENT", False)
        app._YT_QUOTA_SPENT = True
        cached = [{"video_id": "abc", "title": "Villain (2002)"}]
        with patch.object(app, "_youtube_full_movie_cached", return_value=cached):
            self.assertEqual(app.youtube_full_movie("Villain", "2002", "movie"), cached)

    def test_the_quota_latch_stops_the_search_itself(self):
        """A title that has never been looked up is refused before any request
        is made, so the three search queries a lookup costs are not spent on a
        request that can only fail."""
        self.addCleanup(setattr, app, "_YT_QUOTA_SPENT", False)
        app._YT_QUOTA_SPENT = True
        undecorated = app._youtube_full_movie_cached.__wrapped__
        with self.assertRaises(app._YouTubeSearchError) as caught:
            undecorated("Villain", "2002", "movie", "", 0, False, "", False, "")
        self.assertEqual(caught.exception.status, "quota")

    def test_the_quota_message_says_the_title_was_not_checked(self):
        """A limit on YouTube's API must not read as a film being unavailable,
        and the message has to say plainly that nothing was looked up."""
        message = app._yt_status_message("quota")
        self.assertIn("exceeded", message)
        self.assertIn("not checked", message)

    def test_not_saying_a_trusted_movie_exists_when_none_was_found(self):
        """The empty list means the trusted channels had nothing qualifying,
        which is a different claim from never having searched."""
        self.assertIn("trusted", app._yt_not_found_message())
        self.assertIn("trusted", app._yt_not_found_message("tamil"))
        self.assertIn("trusted", app._yt_not_found_message("", official_only=True))
        # The hint for widening the search must survive the rewording.
        self.assertIn("Include uploads from unverified channels", app._yt_not_found_message("", True))

    def test_generic_channel_words_do_not_earn_a_trust_badge(self):
        """'&TV' reduces to the single token 'tv', so the subset match badged any
        channel with 'TV' in its name as a broadcaster."""
        self.assertEqual(app._channel_tier("Crazy Toon TV"), "")
        self.assertEqual(app._channel_tier("Random Movies TV"), "")
        # Real broadcasters and studios keep their badges.
        self.assertEqual(app._channel_tier("Jaya TV"), "tv")
        self.assertEqual(app._channel_tier("Kalaingar TV"), "tv")
        self.assertEqual(app._channel_tier("Star Vijay Tamil"), "tv")
        self.assertIn(app._channel_tier("Shemaroo Movies"), ("studio", "movie_official"))
        self.assertIn(app._channel_tier("Goldmines"), ("studio", "movie_official"))

    def test_runtime_badge_reflects_whether_a_runtime_existed(self):
        """OMDb having no record must not be shown as a passed check."""
        self.assertIn("Runtime ✓", app._trust_badges("tv", runtime_verified=True))
        self.assertIn("Runtime unverified", app._trust_badges("tv", runtime_verified=False))
        self.assertNotIn("Runtime unverified", app._trust_badges("tv", runtime_verified=True))
        # None = checked by episode count instead of length (series playlists),
        # so no runtime chip at all.
        neither = app._trust_badges("tv", runtime_verified=None)
        self.assertNotIn("Runtime", neither)
        self.assertIn("TV Channel", neither)

    def test_missing_omdb_runtime_does_not_block_youtube(self):
        """OMDb has no entry for plenty of regional films (Samuthiram). Reporting
        zero results then would hide a film that is really on YouTube, so the
        runtime check is skipped rather than failed."""
        self.assertEqual(app._omdb_status({}, "Movie not found!"), "not_found")
        message = app._omdb_status_message("not_found")
        self.assertNotIn("cannot be runtime-verified", message)
        self.assertIn("weren't", message)
        self.assertIn("not_found", app._OMDB_BENIGN_STATES)
        self.assertNotIn("rate_limited", app._OMDB_BENIGN_STATES)

    def test_runtime_mismatch_is_still_dropped_when_a_runtime_exists(self):
        """Skipping the check for unknown runtimes must not weaken the real one."""
        video = {
            "id": "vid-1",
            "snippet": {
                "channelId": "UC-sony",
                "channelTitle": "Sony VIZHA",
                "defaultAudioLanguage": "ta",
                "title": "Samuthiram | Full Movie",
                "description": "",
            },
            "contentDetails": {"duration": "PT2H58M"},
            "status": {
                "privacyStatus": "public",
                "embeddable": True,
                "uploadStatus": "processed",
            },
        }
        search_item = {
            "id": {"videoId": "vid-1"},
            "snippet": {
                "title": video["snippet"]["title"],
                "channelTitle": "Sony VIZHA",
                "channelId": "UC-sony",
                "thumbnails": {},
            },
        }

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

            def __exit__(self, *args):
                return False

            def get(self, url, params):
                if url == app.YOUTUBE_SEARCH_URL:
                    return FakeResponse({"items": [search_item]})
                if url == app.YOUTUBE_VIDEOS_URL:
                    return FakeResponse({"items": [video]})
                raise AssertionError(f"Unexpected URL: {url}")

        app.youtube_full_movie.clear()
        with (
            patch.object(app.httpx, "Client", FakeClient),
            patch.object(app, "YOUTUBE_API_KEY", "youtube-key"),
        ):
            # 178 min against an OMDb runtime of 180 is inside the 10% band.
            kept = app._youtube_full_movie_cached(
                "Samuthiram", "2001", "movie", "", 180, False, "in-band",
            )
            # 178 min against a 300 min runtime is a compilation, not the film.
            dropped = app._youtube_full_movie_cached(
                "Samuthiram", "2001", "movie", "", 300, False, "out-of-band",
            )
            # Same video, no reference runtime: listed, but flagged.
            unverified = app._youtube_full_movie_cached(
                "Samuthiram", "2001", "movie", "", 0, False, "unverified",
            )
        app.youtube_full_movie.clear()
        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["runtime_verified"])
        self.assertEqual(dropped, [])
        self.assertEqual(len(unverified), 1)
        self.assertFalse(unverified[0]["runtime_verified"])

    def test_result_limit_allows_more_than_two_results(self):
        self.assertGreaterEqual(app.YOUTUBE_MOVIE_RESULT_LIMIT, 5)
        self.assertFalse(app._ALLOW_UNTRUSTED_VERIFIED)

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
