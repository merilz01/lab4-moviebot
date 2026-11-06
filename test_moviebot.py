import copy
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# фейк значения
os.environ["TELEGRAM_TOKEN"] = "123456:test-token"
os.environ["KINOPOISK_TOKEN"] = "test-token"
os.environ["ZEXKORA_TOKEN"] = "test-token"

import ai
import bot
import database
import kinopoisk
import run

MOVIE = {
    "id": 1,
    "name": "КОСМОС",
    "year": 2020,
    "rating": {"kp": 8.0},
    "description": "История путешествия",
    "genres": [{"name": "фантастика"}],
    "countries": [{"name": "США"}],
    "typeNumber": 1,
}


def llm_response(data):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(message=SimpleNamespace(content=json.dumps(data)))
        ]
    )


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.db_patch = patch.object(
            database, "DB_PATH", Path(self.temp.name) / "test.db"
        )
        self.db_patch.start()
        database.init_db()

    def tearDown(self):
        self.db_patch.stop()
        self.temp.cleanup()

    def test_history_is_private_and_clearable(self):
        database.add_history(1, MOVIE, "genre", "фантастика")
        self.assertEqual(len(database.get_history(1)), 1)
        self.assertEqual(database.get_history(2), [])
        database.clear_history(1)
        self.assertEqual(database.get_history(1), [])

    def test_favorites_are_unique(self):
        self.assertTrue(database.add_favorite(1, MOVIE))
        self.assertFalse(database.add_favorite(1, MOVIE))
        database.clear_favorites(1)
        self.assertEqual(database.get_favorites(1), [])

    def test_rating_updates_instead_of_duplicating(self):
        database.add_rating(1, MOVIE, 5)
        database.add_rating(1, MOVIE, 9)
        self.assertEqual(database.get_user_rating_for_movie(1, 1), 9)
        self.assertEqual(
            database.get_movie_user_rating(1, min_votes=1), (9.0, 1)
        )
        with self.assertRaises(ValueError):
            database.add_rating(1, MOVIE, 11)

    def test_cache_search_handles_cyrillic_case(self):
        database.save_movies_to_cache([MOVIE])
        self.assertEqual(
            database.search_movies_by_keywords(["космос"])[0]["id"], 1
        )

    def test_cache_search_treats_wildcards_literally(self):
        database.save_movies_to_cache([MOVIE])
        self.assertEqual(database.search_movies_by_keywords(["%"]), [])

    def test_nullable_api_fields_can_be_saved(self):
        movie = dict(MOVIE, rating=None, genres=None)
        database.add_history(1, movie, "genre")
        database.save_movies_to_cache([movie])

    def test_settings_survive_another_python_process(self):
        settings = {"rating": "8-10", "year": "2000-2026", "movies_only": True}
        database.save_user_settings(1, settings)
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import json,sys,database; database.DB_PATH=sys.argv[1]; "
                "print(json.dumps(database.get_user_settings(1)))",
                str(database.DB_PATH),
            ],
            cwd=Path(__file__).parent,
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads(result.stdout), settings)

    def test_settings_are_private_and_reset_independently(self):
        database.save_user_settings(1, {"rating": "8-10", "year": ""})
        database.save_user_settings(2, {"rating": "6-10"})
        self.assertEqual(database.get_user_settings(1)["year"], "")
        database.clear_user_settings(1)
        self.assertEqual(database.get_user_settings(1), {})
        self.assertEqual(database.get_user_settings(2)["rating"], "6-10")

    def test_invalid_settings_do_not_overwrite_saved_values(self):
        database.save_user_settings(1, {"rating": "8-10"})
        with self.assertRaises(ValueError):
            database.save_user_settings(1, {"rating": "11-15"})
        self.assertEqual(database.get_user_settings(1)["rating"], "8-10")

    def test_migration_preserves_existing_history(self):
        database.add_history(1, MOVIE, "genre")
        with database.connect_db() as conn:
            conn.execute("DROP TABLE user_settings")
        database.init_db()
        self.assertEqual(len(database.get_history(1)), 1)
        database.save_user_settings(1, {"movies_only": True})
        self.assertTrue(database.get_user_settings(1)["movies_only"])

    def test_failed_transaction_is_rolled_back(self):
        with self.assertRaises(sqlite3.OperationalError):
            with database.connect_db() as conn:
                conn.execute("INSERT INTO user_settings (user_id) VALUES (99)")
                conn.execute("INSERT INTO missing_table VALUES (1)")
        self.assertEqual(database.get_user_settings(99), {})

    @patch.object(bot.bot, "send_message")
    def test_filter_buttons_persist_and_reset_values(self, send):
        message = SimpleNamespace(
            chat=SimpleNamespace(id=1), text="⭐ Рейтинг 8+"
        )
        bot.apply_rating(message)
        message.text = "📅 Любой год"
        bot.apply_year(message)
        bot.toggle_movies_only(message)
        self.assertEqual(
            database.get_user_settings(1),
            {
                "rating": "8-10",
                "year": "",
                "movies_only": True,
            },
        )
        bot.reset_filters(message)
        self.assertEqual(database.get_user_settings(1), {})


class SearchTests(unittest.TestCase):
    @patch("kinopoisk.requests.get")
    def test_empty_response_differs_from_failure(self, get):
        get.return_value.json.return_value = {"docs": [], "pages": 0}
        self.assertEqual(kinopoisk.find_movies(["драма"]), [])
        get.side_effect = kinopoisk.requests.ConnectionError()
        with self.assertRaises(kinopoisk.MovieAPIError):
            kinopoisk.find_movies(["драма"])

    @patch("kinopoisk.time.sleep")
    @patch("kinopoisk.requests.get", side_effect=kinopoisk.requests.Timeout())
    def test_timeout_retries_then_reports_failure(self, get, sleep):
        with self.assertRaises(kinopoisk.MovieAPIError):
            kinopoisk.find_movies(["драма"])
        self.assertEqual(get.call_count, 3)

    @patch("kinopoisk.requests.get")
    def test_malformed_response_reports_failure(self, get):
        get.return_value.json.return_value = {"docs": {"unexpected": 1}}
        with self.assertRaises(kinopoisk.MovieAPIError):
            kinopoisk.find_movies(["драма"])

    @patch("kinopoisk.requests.get")
    def test_narrow_search_starts_at_first_page(self, get):
        get.return_value.json.return_value = {"docs": [MOVIE], "pages": 1}
        self.assertEqual(
            kinopoisk.find_movies(["фантастика"], country="!Россия"), [MOVIE]
        )
        self.assertEqual(get.call_args.kwargs["params"]["page"], 1)
        self.assertEqual(
            get.call_args.kwargs["params"]["countries.name"], "!Россия"
        )

    @patch("kinopoisk.find_movies", return_value=[MOVIE])
    def test_genre_search_excludes_shown_movies(self, find):
        self.assertIsNone(
            kinopoisk.find_random_movie(["фантастика"], exclude_ids={1})
        )

    def test_documentaries_and_music_are_available(self):
        for genre in ("документальный", "музыка"):
            self.assertFalse(
                kinopoisk.is_bad_movie(dict(MOVIE, genres=[{"name": genre}]))
            )

    def test_regular_movies_with_show_in_title_are_available(self):
        for name in ("Побег из Шоушенка", "Шоу Трумана"):
            self.assertFalse(kinopoisk.is_bad_movie(dict(MOVIE, name=name)))

    def test_cache_obeys_filters(self):
        params = {
            "genres": ["фантастика"],
            "rating": "6-10",
            "year": None,
            "country": "!Россия",
        }
        self.assertTrue(bot.matches_filters(MOVIE, params, {}))
        self.assertFalse(
            bot.matches_filters(MOVIE, params, {"rating": "9-10"})
        )
        self.assertFalse(
            bot.matches_filters(MOVIE, params, {"year": "2000-2010"})
        )
        self.assertFalse(
            bot.matches_filters(
                dict(MOVIE, typeNumber=2), params, {"movies_only": True}
            )
        )
        self.assertFalse(
            bot.matches_filters(
                dict(MOVIE, countries=[{"name": "Россия"}]), params, {}
            )
        )

    def test_any_year_overrides_year_from_mood(self):
        params = {
            "genres": ["фантастика"],
            "rating": "6-10",
            "year": "2000-2010",
            "country": None,
        }
        self.assertFalse(bot.matches_filters(MOVIE, params, {}))
        self.assertTrue(bot.matches_filters(MOVIE, params, {"year": ""}))


class DialogTests(unittest.TestCase):
    def setUp(self):
        bot.user_last_mood.clear()
        bot.user_last_movie.clear()
        self.message = SimpleNamespace(
            chat=SimpleNamespace(id=1), text=bot.BACK_BUTTON
        )

    @patch.object(bot.bot, "send_message")
    @patch("bot.add_rating", side_effect=sqlite3.OperationalError("locked"))
    def test_failed_rating_does_not_claim_success(self, add, send):
        self.message.text = "8"
        bot.user_last_movie[1] = MOVIE
        bot.save_rating(self.message)
        self.assertIn("Не удалось", send.call_args.args[1])
        self.assertNotIn("сохранена", send.call_args.args[1])

    @patch.object(bot.bot, "send_message")
    @patch("bot.get_user_settings", side_effect=sqlite3.OperationalError())
    def test_settings_read_failure_is_explained(self, get, send):
        bot.show_filters(self.message)
        self.assertIn("Не удалось", send.call_args.args[1])

    @patch.object(bot.bot, "send_message")
    @patch(
        "bot.find_random_movie",
        side_effect=kinopoisk.MovieAPIError("API недоступен"),
    )
    @patch("bot.get_user_settings", return_value={})
    def test_api_failure_is_not_presented_as_empty_search(
        self, settings, find, send
    ):
        self.message.text = "Драма"
        bot.handle_genre(self.message)
        self.assertEqual(send.call_args.args[1], "API недоступен")

    @patch.object(bot.bot, "send_message")
    @patch("bot.mood_to_params")
    def test_back_cancels_mood_input(self, mood, send):
        bot.process_mood(self.message)
        mood.assert_not_called()
        self.assertIn("Выбери жанр", send.call_args.args[1])

    @patch.object(bot.bot, "send_message")
    @patch("bot.add_rating")
    def test_back_cancels_rating(self, add, send):
        bot.save_rating(self.message)
        add.assert_not_called()
        self.assertIn("Выбери жанр", send.call_args.args[1])

    @patch.object(bot.bot, "register_next_step_handler")
    @patch.object(bot.bot, "send_message")
    def test_nontext_rating_retries(self, send, register):
        self.message.text = None
        bot.save_rating(self.message)
        register.assert_called_once_with(self.message, bot.save_rating)

    @patch.object(bot.bot, "send_photo")
    @patch.object(bot.bot, "send_message")
    @patch("bot.get_movie_user_rating", return_value=(None, 0))
    @patch("bot.get_user_rating_for_movie", return_value=None)
    def test_long_card_uses_text_message(self, rating, average, send, photo):
        movie = copy.deepcopy(MOVIE)
        movie.update(
            description="Я" * 700,
            poster={"url": "https://example.com/poster.jpg"},
        )
        bot.send_movie_card(1, movie, mood_text="Я" * 500, reason="Т" * 250)
        photo.assert_not_called()
        send.assert_called_once()

    @patch.object(bot.bot, "send_message")
    @patch("bot.get_history")
    def test_long_history_is_split_into_telegram_messages(self, history, send):
        history.return_value = [("Название", 2020, 8, "mood", "Я" * 500)] * 10
        bot.show_history(self.message)
        self.assertGreater(send.call_count, 1)
        for call in send.call_args_list:
            self.assertLessEqual(
                len(call.args[1].encode("utf-16-le")) // 2, 4096
            )


class StartupTests(unittest.TestCase):
    @patch("run.validate_config", side_effect=ValueError("Нет токена"))
    def test_missing_config_exits_with_failure(self, validate):
        with self.assertLogs(level="ERROR"):
            self.assertEqual(run.main(), 1)

    @patch("run.validate_config")
    @patch("database.init_db", side_effect=sqlite3.OperationalError())
    def test_database_failure_does_not_start_polling(self, init, validate):
        with patch.object(bot.bot, "infinity_polling") as polling:
            with self.assertLogs(level="ERROR"):
                self.assertEqual(run.main(), 1)
            polling.assert_not_called()


class AITests(unittest.TestCase):
    @patch("ai.call_llm", side_effect=ai.MoodServiceError("Недоступен"))
    def test_service_failure_is_not_treated_as_bad_mood(self, call):
        with self.assertRaises(ai.MoodServiceError):
            ai.mood_to_params("Хочу драму")

    @patch("ai.call_llm")
    def test_bad_model_parameters_are_normalized(self, call):
        call.return_value = llm_response(
            {
                "genres": ["драма"],
                "rating": "99-100",
                "keywords": None,
                "year": [],
            }
        )
        params = ai.mood_to_params("Хочу драму")
        self.assertEqual(params["rating"], "6.5-10")
        self.assertEqual(params["keywords"], [])
        self.assertIsNone(params["year"])

    @patch("ai.call_llm")
    def test_nonobject_model_output_does_not_crash(self, call):
        call.return_value = llm_response([])
        self.assertIsNone(ai.mood_to_params("Хочу драму"))

    @patch("ai.call_llm")
    def test_boolean_is_not_movie_index(self, call):
        call.return_value = llm_response({"index": True, "reason": "test"})
        self.assertIsNone(ai.pick_best_movie([MOVIE, MOVIE], "test"))


if __name__ == "__main__":
    unittest.main()
