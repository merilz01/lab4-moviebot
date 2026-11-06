import json
import logging
import re
from datetime import date

from openai import APIError, OpenAI

from config import ZEXKORA_TOKEN
from errors import MoodServiceError
from moods import GENRES

# Основной и резервный серверы Zexkora
PRIMARY_URL = "https://api.zexkora.cc/v1"
FALLBACK_URL = "https://2api.zexkora.cc/v1"

# Модель
MODEL_NAME = "deepseek-v4.1-flash"


def call_llm(messages: list, max_tokens: int):
    """
    Пробует основной сервер Zexkora, если не работает — резервный.
    """
    last_error = None
    for url in [PRIMARY_URL, FALLBACK_URL]:
        try:
            with OpenAI(
                api_key=ZEXKORA_TOKEN, base_url=url, timeout=30, max_retries=0
            ) as client:
                response = client.chat.completions.create(
                    model=MODEL_NAME,
                    messages=messages,
                    max_tokens=max_tokens,
                    temperature=0.3,
                )
            logging.debug("LLM ответил через %s (%s)", url, MODEL_NAME)
            return response
        except APIError as e:
            last_error = e
            print(f"Сервис настроения недоступен: {type(e).__name__}")
            continue
    raise MoodServiceError(
        "Сервис подбора по настроению недоступен. "
        "Попробуй позже или выбери жанр вручную."
    ) from last_error


MOOD_PROMPT = """Ты — помощник по подбору фильмов.
Пользователь описал настроение. Выбери 1-3 подходящих жанра ТОЛЬКО из этого
списка:
{genres}

И предложи диапазон рейтинга Кинопоиска (числа от 0 до 10, формат "min-max").

Также выдели 2-4 КЛЮЧЕВЫХ СЛОВА из запроса пользователя — существительные,
которые описывают тему (например, "зомби", "замок", "космос", "любовь",
"школа").
Не включай общие слова: "фильм", "хочу", "посмотреть", "настроение", "что-то".

Если пользователь упомянул ГОД или период — верни поле "year" в формате
"min-max".
Если год не упомянут — верни "year": null.

Если пользователь упомянул СТРАНУ — верни поле "country":
- «российский», «русский», «отечественный» → "Россия"
- «иностранный», «зарубежный», «не российский» → "!Россия"
- «американский» → "США"
- «французский» → "Франция"
- «корейский» → "Южная Корея"
- «японский» → "Япония"
Если страна не упомянута — верни "country": null.

ВАЖНО: не добавляй жанр "комедия", если пользователь явно не просит смешное.
ВАЖНО: не рассуждай, верни ТОЛЬКО валидный JSON без пояснений:
{{"genres": ["жанр1"], "rating": "6.5-10", "year": null, "country": null,
"keywords": ["слово1", "слово2"]}}

Примеры:
- «фильм про зомби в замке» →
{{"genres": ["ужасы"], "rating": "6.5-10", "year": null, "country": null,
"keywords": ["зомби", "замок"]}}
- «российский фильм 2007 года про любовь» →
{{"genres": ["мелодрама"], "rating": "6.5-10", "year": "2007-2007",
"country": "Россия", "keywords": ["любовь"]}}
- «осеннее настроение» →
{{"genres": ["драма"], "rating": "6.5-10", "year": null, "country": null,
"keywords": []}}

Настроение пользователя: {user_text}"""

PICK_PROMPT_STRICT = """Пользователь хочет посмотреть фильм под настроение:
"{mood}"

Вот список фильмов, найденных в базе:
{movies}

Выбери ОДИН фильм, который ТОЧНО подходит под настроение.
Если ТОЧНОГО попадания нет — верни index: -1.

Верни ТОЛЬКО валидный JSON без пояснений:
{{"index": 0, "reason": "почему подходит"}}

Или, если точного попадания нет:
{{"index": -1, "reason": "точного попадания нет"}}

index — это номер фильма из списка (нумерация с 0)."""

PICK_PROMPT_SOFT = """Пользователь хочет посмотреть фильм под настроение:
"{mood}"

Вот список фильмов, найденных в базе:
{movies}

Выбери ОДИН фильм — НАИБОЛЕЕ близкий к настроению.
Не отказывайся от выбора: даже если точного попадания нет, выбери самый
подходящий вариант.

Верни ТОЛЬКО валидный JSON без пояснений:
{{"index": 0, "reason": "почему ближе всего"}}

index — это номер фильма из списка (нумерация с 0)."""


def parse_json(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Ожидался JSON-объект")
    return data


def valid_range(value, low, high, default=None):
    if not isinstance(value, str) or not re.fullmatch(
        r"\d+(?:\.\d+)?-\d+(?:\.\d+)?", value
    ):
        return default
    start, end = map(float, value.split("-"))
    return value if low <= start <= end <= high else default


def mood_to_params(user_text: str) -> dict | None:
    if not ZEXKORA_TOKEN:
        return None

    prompt = MOOD_PROMPT.format(genres=", ".join(GENRES), user_text=user_text)

    try:
        response = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
        )
        text = (response.choices[0].message.content or "").strip()
        data = parse_json(text)
    except (
        ValueError,
        AttributeError,
        IndexError,
        TypeError,
    ) as e:
        logging.warning("Неверный ответ ИИ: %s", type(e).__name__)
        return None

    genres = data.get("genres")
    valid_genres = (
        [g for g in genres if g in GENRES][:3]
        if isinstance(genres, list)
        else []
    )
    if not valid_genres:
        logging.warning("В ответе ИИ нет допустимых жанров")
        return None

    keywords = data.get("keywords")
    keywords = (
        [
            kw.strip()[:50]
            for kw in keywords
            if isinstance(kw, str) and kw.strip()
        ][:4]
        if isinstance(keywords, list)
        else []
    )
    country = data.get("country")
    country = (
        country.strip()
        if isinstance(country, str) and country.strip()
        else None
    )
    if country == "Южная Корея":
        country = "Корея Южная"
    return {
        "genres": valid_genres,
        "rating": valid_range(data.get("rating"), 0, 10, "6.5-10"),
        "year": valid_range(data.get("year"), 1800, date.today().year),
        "country": country,
        "keywords": keywords,
    }


def pick_best_movie(movies: list[dict], mood: str, strict: bool = True):
    if not ZEXKORA_TOKEN or not movies:
        return None

    lines = []
    for i, m in enumerate(movies):
        name = m.get("name") or m.get("alternativeName") or "?"
        year = m.get("year", "?")
        genres = ", ".join(g.get("name", "") for g in (m.get("genres") or []))[
            :80
        ]
        countries = ", ".join(
            c.get("name", "") for c in (m.get("countries") or [])
        )[:50]
        desc = (m.get("description") or "")[:120]
        lines.append(f"{i}. {name} ({year}) [{genres}] [{countries}] — {desc}")

    template = PICK_PROMPT_STRICT if strict else PICK_PROMPT_SOFT
    prompt = template.format(mood=mood, movies="\n".join(lines))

    try:
        response = call_llm(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
        )
        msg = response.choices[0].message
        text = (msg.content or "").strip()

        if not text:
            print("=== pick_best_movie: пустой content ===")
            return None

        data = parse_json(text)
    except (
        ValueError,
        AttributeError,
        IndexError,
        TypeError,
    ) as e:
        logging.warning("Неверный ответ ИИ: %s", type(e).__name__)
        return None

    idx = data.get("index")
    reason = data.get("reason", "")
    reason = reason[:250] if isinstance(reason, str) else ""

    if idx == -1:
        return "no_match"

    if type(idx) is not int or idx < 0 or idx >= len(movies):
        logging.warning("ИИ вернул недопустимый индекс кандидата")
        return None

    return movies[idx], reason
