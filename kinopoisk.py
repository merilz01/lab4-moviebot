import random
import re
import time

import requests

from config import KINOPOISK_TOKEN
from errors import MovieAPIError
from moods import DEFAULT_RATING

API_URL = "https://api.kinopoisk.dev/v1.4/movie"

# отсеиваем лишнее
BAD_KEYWORDS = [
    "концерт",
    "стендап",
    "stand-up",
    "standup",
    "фестиваль",
    "церемония",
]

# отсеиваем жанры
BAD_GENRES = {
    "концерт",
    "ток-шоу",
    "реальное тв",
}


def truncate(text: str, max_len: int = 700) -> str:
    if not text:
        return ""
    if len(text) <= max_len:
        return text

    cut = text[:max_len]
    last_dot = cut.rfind(". ")
    if last_dot > max_len * 0.5:
        return cut[: last_dot + 1]
    return cut.rstrip() + "…"


def is_bad_movie(movie: dict) -> bool:
    text = (
        (movie.get("name") or "") + " " + (movie.get("alternativeName") or "")
    ).lower()
    if any(
        re.search(r"\b" + re.escape(kw) + r"\b", text) for kw in BAD_KEYWORDS
    ):
        return True

    genres = {g.get("name", "").lower() for g in (movie.get("genres") or [])}
    if genres & BAD_GENRES:
        return True

    return False


def find_movies(
    genres,
    rating=DEFAULT_RATING,
    year=None,
    country=None,
    type_number=None,
    limit=30,
):
    """Пустая выдача — []; сбой сети или ответа — MovieAPIError."""
    headers = {"X-API-KEY": KINOPOISK_TOKEN}
    query = {
        "genres.name": genres,
        "rating.kp": rating,
        "limit": limit,
        "page": 1,
    }
    if year:
        query["year"] = year
    if country:
        query["countries.name"] = country
    if type_number is not None:
        query["typeNumber"] = type_number

    for attempt in range(3):
        try:
            query["page"] = 1
            timeout = 20 if attempt == 0 else 30
            response = requests.get(
                API_URL, headers=headers, params=query, timeout=timeout
            )
            response.raise_for_status()
            data = parse_response(response)
            pages = data.get("pages", 1)
            if isinstance(pages, int) and pages > 1:
                query["page"] = random.randint(1, min(pages, 3))
                if query["page"] != 1:
                    response = requests.get(
                        API_URL, headers=headers, params=query, timeout=timeout
                    )
                    response.raise_for_status()
                    data = parse_response(response)
            docs = data.get("docs") or []
            docs = [
                m
                for m in docs
                if isinstance(m, dict) and m.get("id") and not is_bad_movie(m)
            ]
            return docs
        except requests.Timeout:
            print(f"Попытка {attempt + 1}: Таймаут API (timeout={timeout}s)")
            if attempt < 2:
                time.sleep(2)  # Пауза перед повторной попыткой
        except (requests.RequestException, ValueError, AttributeError) as e:
            raise MovieAPIError(
                "Сервис фильмов недоступен или отклонил запрос. "
                "Попробуй позже; проверь подключение и ключ API."
            ) from e

    raise MovieAPIError("Сервис фильмов не ответил вовремя. Попробуй позже.")


def parse_response(response):
    """Проверяет структуру JSON до использования данных внешнего API."""
    data = response.json()
    if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
        raise MovieAPIError("Сервис фильмов вернул некорректные данные.")
    return data


def find_random_movie(
    genres,
    rating=DEFAULT_RATING,
    year=None,
    country=None,
    type_number=None,
    exclude_ids=None,
):
    docs = find_movies(genres, rating, year, country, type_number)
    docs = [m for m in docs if m.get("id") not in (exclude_ids or set())]
    return random.choice(docs) if docs else None


def get_poster_url(movie: dict) -> str | None:
    poster = movie.get("poster")
    if not poster:
        return None
    return poster.get("url") or poster.get("previewUrl")


def get_kinopoisk_url(movie: dict) -> str | None:
    kp_id = movie.get("id")
    if not kp_id:
        return None
    return f"https://www.kinopoisk.ru/film/{kp_id}/"
