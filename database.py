import json as json_module
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH


@contextmanager
def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def get_user_settings(user_id: int) -> dict:
    with connect_db() as conn:
        row = conn.execute(
            "SELECT rating, year, movies_only FROM user_settings "
            "WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    if row is None:
        return {}
    settings = {"movies_only": bool(row[2])}
    for key, value in zip(("rating", "year"), row[:2]):
        if value is not None:
            settings[key] = value
    return settings


def save_user_settings(user_id: int, settings: dict):
    for key, minimum, maximum in (("rating", 0, 10), ("year", 1800, 9999)):
        value = settings.get(key)
        # Пустой год означает явный выбор «Любой год», NULL — нет выбора.
        if value is None or (key == "year" and value == ""):
            continue
        if not isinstance(value, str) or not re.fullmatch(
            r"\d+(?:\.\d+)?-\d+(?:\.\d+)?", value
        ):
            raise ValueError("Некорректный диапазон фильтра")
        low, high = map(float, value.split("-"))
        if not minimum <= low <= high <= maximum:
            raise ValueError("Диапазон фильтра вне допустимых границ")
    movies_only = settings.get("movies_only", False)
    if type(movies_only) is not bool:
        raise ValueError("Режим фильмов должен быть логическим значением")
    with connect_db() as conn:
        conn.execute(
            """
            INSERT INTO user_settings (user_id, rating, year, movies_only)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                rating = excluded.rating, year = excluded.year,
                movies_only = excluded.movies_only
        """,
            (
                user_id,
                settings.get("rating"),
                settings.get("year"),
                int(movies_only),
            ),
        )


def clear_user_settings(user_id: int):
    with connect_db() as conn:
        conn.execute("DELETE FROM user_settings WHERE user_id = ?", (user_id,))


def init_db():
    with connect_db() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                movie_id INTEGER,
                movie_name TEXT,
                movie_year TEXT,
                movie_rating REAL,
                query_type TEXT,
                query_text TEXT,
                created_at TEXT,
                hidden INTEGER DEFAULT 0
            )
        """)

        columns = {
            row[1] for row in cursor.execute("PRAGMA table_info(history)")
        }
        if "hidden" not in columns:
            cursor.execute(
                "ALTER TABLE history ADD COLUMN hidden INTEGER DEFAULT 0"
            )

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                movie_id INTEGER,
                movie_name TEXT,
                movie_year TEXT,
                movie_rating REAL,
                created_at TEXT,
                UNIQUE(user_id, movie_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                movie_id INTEGER,
                movie_name TEXT,
                rating INTEGER,
                created_at TEXT,
                UNIQUE(user_id, movie_id)
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS movies_cache (
                id INTEGER PRIMARY KEY,
                name TEXT,
                year TEXT,
                rating REAL,
                description TEXT,
                genres TEXT,
                poster_url TEXT,
                kp_url TEXT,
                raw_json TEXT,
                created_at TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                user_id INTEGER PRIMARY KEY,
                rating TEXT,
                year TEXT,
                movies_only INTEGER NOT NULL DEFAULT 0
                    CHECK (movies_only IN (0, 1))
            )
        """)


# История


def add_history(
    user_id: int, movie: dict, query_type: str, query_text: str = ""
):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO history
                (user_id, movie_id, movie_name, movie_year, movie_rating,
                 query_type, query_text, created_at, hidden)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
            (
                user_id,
                movie.get("id"),
                movie.get("name") or movie.get("alternativeName"),
                str(movie.get("year", "?")),
                (movie.get("rating") or {}).get("kp"),
                query_type,
                query_text,
                datetime.now().isoformat(),
            ),
        )


def get_history(user_id: int, limit: int = 10):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT movie_name, movie_year, movie_rating, query_type, query_text
            FROM history
            WHERE user_id = ? AND hidden = 0
            ORDER BY id DESC
            LIMIT ?
        """,
            (user_id, limit),
        )
        rows = cursor.fetchall()
        return rows


def clear_history(user_id: int):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE history SET hidden = 1 WHERE user_id = ?", (user_id,)
        )


# Избранное


def add_favorite(user_id: int, movie: dict) -> bool:
    with connect_db() as conn:
        cursor = conn.cursor()
        try:
            cursor.execute(
                """
                INSERT INTO favorites
                    (user_id, movie_id, movie_name, movie_year,
                     movie_rating, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """,
                (
                    user_id,
                    movie.get("id"),
                    movie.get("name") or movie.get("alternativeName"),
                    str(movie.get("year", "?")),
                    (movie.get("rating") or {}).get("kp"),
                    datetime.now().isoformat(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def get_favorites(user_id: int, limit: int = 20):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT movie_name, movie_year, movie_rating
            FROM favorites
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
        """,
            (user_id, limit),
        )
        rows = cursor.fetchall()
        return rows


def clear_favorites(user_id: int):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM favorites WHERE user_id = ?", (user_id,))


# оценки


def add_rating(user_id: int, movie: dict, rating: int):
    """Одна оценка пользователя на фильм; повторная запись заменяет её."""
    if type(rating) is not int or not 1 <= rating <= 10:
        raise ValueError("Оценка должна быть от 1 до 10")
    with connect_db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO ratings
                (user_id, movie_id, movie_name, rating, created_at)
            VALUES (?, ?, ?, ?, ?)
        """,
            (
                user_id,
                movie.get("id"),
                movie.get("name") or movie.get("alternativeName"),
                rating,
                datetime.now().isoformat(),
            ),
        )


def get_movie_user_rating(movie_id: int, min_votes: int = 3):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT AVG(rating), COUNT(*)
            FROM ratings
            WHERE movie_id = ?
        """,
            (movie_id,),
        )
        row = cursor.fetchone()
        if not row or row[1] == 0:
            return None, 0
        avg, count = row
        if count < min_votes:
            return None, count
        return round(avg, 1), count


def get_user_rating_for_movie(user_id: int, movie_id: int):
    with connect_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT rating FROM ratings
            WHERE user_id = ? AND movie_id = ?
        """,
            (user_id, movie_id),
        )
        row = cursor.fetchone()
        return row[0] if row else None


# Кэш фильмов и поиск по ключевым словам


def save_movies_to_cache(movies: list):
    with connect_db() as conn:
        cursor = conn.cursor()
        for m in movies:
            movie_id = m.get("id")
            if not movie_id:
                continue
            genres_str = ", ".join(
                g.get("name", "") for g in (m.get("genres") or [])
            )
            poster = m.get("poster") or {}
            poster_url = poster.get("url") or poster.get("previewUrl") or ""
            cursor.execute(
                """
                INSERT OR REPLACE INTO movies_cache
                (id, name, year, rating, description, genres, poster_url,
                 kp_url, raw_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    movie_id,
                    m.get("name") or m.get("alternativeName") or "",
                    str(m.get("year", "?")),
                    (m.get("rating") or {}).get("kp"),
                    m.get("description") or m.get("shortDescription") or "",
                    genres_str,
                    poster_url,
                    f"https://www.kinopoisk.ru/film/{movie_id}/",
                    json_module.dumps(m, ensure_ascii=False),
                    datetime.now().isoformat(),
                ),
            )


def search_movies_by_keywords(keywords: list, limit: int = 20):
    if not keywords:
        return []
    with connect_db() as conn:
        conn.create_function(
            "CASEFOLD", 1, lambda value: (value or "").casefold()
        )
        cursor = conn.cursor()
        conditions = []
        params = []
        for kw in keywords:
            conditions.append(
                "(CASEFOLD(name) LIKE ? ESCAPE '\\' "
                "OR CASEFOLD(description) LIKE ? ESCAPE '\\')"
            )
            keyword = (
                kw.casefold()
                .replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            params.extend([f"%{keyword}%", f"%{keyword}%"])
        query = f"""
            SELECT raw_json FROM movies_cache
            WHERE {" OR ".join(conditions)}
            LIMIT ?
        """
        params.append(limit)
        cursor.execute(query, params)
        rows = cursor.fetchall()
        movies = []
        for row in rows:
            try:
                movies.append(json_module.loads(row[0]))
            except (ValueError, TypeError):
                continue
        return movies
