import html as html_module
import logging
import sqlite3
from datetime import date
from functools import wraps

import requests
import telebot
from telebot import apihelper
from telebot.types import KeyboardButton, ReplyKeyboardMarkup

from ai import mood_to_params, pick_best_movie
from config import TELEGRAM_TOKEN, ZEXKORA_TOKEN
from database import (
    add_favorite,
    add_history,
    add_rating,
    clear_favorites,
    clear_history,
    clear_user_settings,
    get_favorites,
    get_history,
    get_movie_user_rating,
    get_user_rating_for_movie,
    get_user_settings,
    save_movies_to_cache,
    save_user_settings,
    search_movies_by_keywords,
)
from errors import MoodServiceError, MovieAPIError
from kinopoisk import (
    find_movies,
    find_random_movie,
    get_kinopoisk_url,
    get_poster_url,
    is_bad_movie,
    truncate,
)
from moods import DEFAULT_RATING, GENRE_EMOJI, GENRES

bot = telebot.TeleBot(TELEGRAM_TOKEN, threaded=False)

# Состояние пользователя
user_last_genre = {}
user_last_mood = {}
user_shown_ids = {}
user_last_movie = {}

# Тексты кнопок
MORE_GENRE_BUTTON = "🎲 Ещё по жанру"
MORE_MOOD_BUTTON = "💭 Ещё по настроению"
MOOD_BUTTON = "💭 Опиши настроение"
FILTERS_BUTTON = "⚙️ Фильтры"
BACK_BUTTON = "🎭 К жанрам"
RESET_FILTERS_BUTTON = "🗑️ Сбросить фильтры"
SET_RATING_BUTTON = "⭐ Настроить рейтинг"
SET_YEAR_BUTTON = "📅 Настроить год"
TOGGLE_MOVIES_ONLY = "🎬 Только фильмы"
BACK_TO_FILTERS_BUTTON = "↩️ Назад к фильтрам"
FAV_BUTTON = "⭐ В избранное"
HISTORY_BUTTON = "📜 История"
FAVORITES_BUTTON = "⭐ Избранное"
RATE_BUTTON = "⭐ Оценить фильм"

RATING_PRESETS = {
    "⭐ Любой рейтинг": "0-10",
    "⭐ Рейтинг 6+": "6-10",
    "⭐ Рейтинг 7+": "7-10",
    "⭐ Рейтинг 8+": "8-10",
}
YEAR_PRESETS = {
    "📅 Любой год": None,
    "📅 После 2000": f"2000-{date.today().year}",
    "📅 После 2010": f"2010-{date.today().year}",
    "📅 После 2020": f"2020-{date.today().year}",
}

MIN_VOTES_FOR_RATING = 1


def handle_errors(func):
    """Обрабатывает ожидаемые сбои и обычных, и пошаговых обработчиков."""

    @wraps(func)
    def wrapped(message, *args, **kwargs):
        try:
            return func(message, *args, **kwargs)
        except (MovieAPIError, MoodServiceError) as error:
            text = str(error)
        except sqlite3.Error as error:
            logging.error("Ошибка SQLite: %s", type(error).__name__)
            text = (
                "Не удалось прочитать или сохранить данные. "
                "Попробуй ещё раз; проверь доступ к movies.db."
            )
        except (apihelper.ApiException, requests.RequestException) as error:
            logging.warning("Ошибка Telegram: %s", type(error).__name__)
            # Повторная отправка при обрыве сети также может не сработать.
            return None
        try:
            bot.send_message(
                message.chat.id, text, reply_markup=genres_keyboard()
            )
        except (apihelper.ApiException, requests.RequestException) as error:
            logging.warning("Ошибка отправки: %s", type(error).__name__)
        return None

    return wrapped


def genres_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(MOOD_BUTTON))
    kb.row(KeyboardButton(FILTERS_BUTTON), KeyboardButton(HISTORY_BUTTON))
    kb.row(KeyboardButton(FAVORITES_BUTTON))
    row = []
    for genre in GENRES:
        row.append(genre.capitalize())
        if len(row) == 3:
            kb.row(*row)
            row = []
    if row:
        kb.row(*row)
    return kb


def after_movie_keyboard(has_mood: bool = False):
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(FAV_BUTTON), KeyboardButton(RATE_BUTTON))
    if has_mood:
        kb.row(KeyboardButton(MORE_MOOD_BUTTON))
    kb.row(KeyboardButton(MORE_GENRE_BUTTON))
    kb.row(KeyboardButton(FILTERS_BUTTON), KeyboardButton(BACK_BUTTON))
    return kb


def filters_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(KeyboardButton(SET_RATING_BUTTON))
    kb.row(KeyboardButton(SET_YEAR_BUTTON))
    kb.row(KeyboardButton(TOGGLE_MOVIES_ONLY))
    kb.row(KeyboardButton(RESET_FILTERS_BUTTON))
    kb.row(KeyboardButton(BACK_BUTTON))
    return kb


def rating_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    for label in RATING_PRESETS:
        kb.row(KeyboardButton(label))
    kb.row(KeyboardButton(BACK_TO_FILTERS_BUTTON))
    return kb


def year_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    for label in YEAR_PRESETS:
        kb.row(KeyboardButton(label))
    kb.row(KeyboardButton(BACK_TO_FILTERS_BUTTON))
    return kb


def rate_keyboard():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row(*[KeyboardButton(str(i)) for i in range(1, 6)])
    kb.row(*[KeyboardButton(str(i)) for i in range(6, 11)])
    kb.row(KeyboardButton(BACK_BUTTON))
    return kb


def send_movie_card(
    chat_id: int,
    movie: dict,
    has_mood: bool = False,
    reason: str = "",
    mood_text: str = "",
):
    name = html_module.escape(
        movie.get("name") or movie.get("alternativeName") or "Без названия"
    )
    year = html_module.escape(str(movie.get("year", "?")))
    rating = (movie.get("rating") or {}).get("kp") or "?"
    desc_raw = (
        movie.get("description")
        or movie.get("shortDescription")
        or "Описание отсутствует."
    )
    desc = html_module.escape(truncate(desc_raw, 700))

    text = ""
    if mood_text:
        text += f"Настроение: {html_module.escape(mood_text)}\n"
    if reason:
        text += f"💡 {html_module.escape(reason)}\n\n"
    text += f"🎬 {name} ({year})\n⭐ Рейтинг Кинопоиска: {rating}\n"

    user_rating, count = get_movie_user_rating(
        movie.get("id"), min_votes=MIN_VOTES_FOR_RATING
    )
    if user_rating is not None:
        text += (
            f"👥 Рейтинг пользователей: {user_rating}/10 ({count} оценок)\n"
        )

    my_rating = get_user_rating_for_movie(chat_id, movie.get("id"))
    if my_rating is not None:
        text += f"⭐ Ваша оценка: {my_rating}/10\n"

    text += f"\n{desc}"

    kp_url = get_kinopoisk_url(movie)
    if kp_url:
        text += f"\n\n🔗 <a href='{kp_url}'>Подробнее на Кинопоиске</a>"

    poster_url = get_poster_url(movie)
    if poster_url and len(text.encode("utf-16-le")) // 2 <= 1024:
        try:
            bot.send_photo(
                chat_id,
                poster_url,
                caption=text,
                parse_mode="HTML",
                reply_markup=after_movie_keyboard(has_mood=has_mood),
            )
            return
        except (apihelper.ApiException, requests.RequestException) as error:
            logging.warning(
                "Не удалось отправить постер: %s", type(error).__name__
            )

    bot.send_message(
        chat_id,
        text,
        parse_mode="HTML",
        reply_markup=after_movie_keyboard(has_mood=has_mood),
    )


def send_movie(chat_id: int, genre: str):
    user_last_mood.pop(chat_id, None)
    filters = get_user_settings(chat_id)
    rating = filters.get("rating", DEFAULT_RATING)
    year = filters.get("year")
    type_number = 1 if filters.get("movies_only") else None

    emoji = GENRE_EMOJI.get(genre, "🎬")
    bot.send_message(chat_id, f"{emoji} Ищу что-нибудь в жанре «{genre}»...")
    movie = find_random_movie(
        [genre],
        rating=rating,
        year=year,
        type_number=type_number,
        exclude_ids=user_shown_ids.get(chat_id),
    )
    if movie:
        movie_id = movie.get("id")
        if movie_id:
            user_shown_ids.setdefault(chat_id, set()).add(movie_id)
        user_last_movie[chat_id] = movie
        try:
            save_movies_to_cache([movie])
        except sqlite3.Error as e:
            logging.warning("Кэш недоступен: %s", type(e).__name__)
        add_history(chat_id, movie, query_type="genre", query_text=genre)
        send_movie_card(chat_id, movie, has_mood=False)
    else:
        bot.send_message(
            chat_id,
            "Ничего не нашлось с такими фильтрами. "
            "Попробуй сбросить их или выбрать другой жанр.",
            reply_markup=filters_keyboard(),
        )


# хендлеры


@bot.message_handler(commands=["start"])
@handle_errors
def start(message):
    name = message.from_user.first_name or "друг"
    bot.send_message(
        message.chat.id,
        f"Привет, {name}! 👋\n"
        "Я помогу подобрать фильм по жанру или настроению 🎬\n\n"
        "Выбери жанр, настрой фильтры или опиши настроение:",
        reply_markup=genres_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text and m.text.lower() in GENRES)
@handle_errors
def handle_genre(message):
    genre = message.text.lower()
    user_last_genre[message.chat.id] = genre
    user_last_mood.pop(message.chat.id, None)
    user_shown_ids.pop(message.chat.id, None)
    send_movie(message.chat.id, genre)


@bot.message_handler(func=lambda m: m.text == MORE_GENRE_BUTTON)
@handle_errors
def handle_more_genre(message):
    genre = user_last_genre.get(message.chat.id)
    if not genre:
        bot.send_message(
            message.chat.id,
            "Сначала выбери жанр:",
            reply_markup=genres_keyboard(),
        )
        return
    send_movie(message.chat.id, genre)


@bot.message_handler(func=lambda m: m.text == BACK_BUTTON)
@handle_errors
def handle_back(message):
    bot.send_message(
        message.chat.id, "Выбери жанр:", reply_markup=genres_keyboard()
    )


# фильтры


@bot.message_handler(func=lambda m: m.text == FILTERS_BUTTON)
@handle_errors
def show_filters(message):
    current = get_user_settings(message.chat.id)
    movies_only = (
        "включено ✅" if current.get("movies_only") else "выключено ❌"
    )
    text = "⚙️ Текущие фильтры:\n"
    text += f"• Рейтинг: {current.get('rating', DEFAULT_RATING)}\n"
    text += f"• Год: {current.get('year') or 'любой'}\n"
    text += f"• Только фильмы: {movies_only}\n"
    bot.send_message(message.chat.id, text, reply_markup=filters_keyboard())


@bot.message_handler(func=lambda m: m.text == SET_RATING_BUTTON)
@handle_errors
def set_rating(message):
    bot.send_message(
        message.chat.id,
        "Выбери минимальный рейтинг:",
        reply_markup=rating_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text in RATING_PRESETS)
@handle_errors
def apply_rating(message):
    filters = get_user_settings(message.chat.id)
    filters["rating"] = RATING_PRESETS[message.text]
    save_user_settings(message.chat.id, filters)
    bot.send_message(
        message.chat.id,
        f"Ок, рейтинг: {message.text}",
        reply_markup=filters_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == SET_YEAR_BUTTON)
@handle_errors
def set_year(message):
    bot.send_message(
        message.chat.id, "Выбери период:", reply_markup=year_keyboard()
    )


@bot.message_handler(func=lambda m: m.text in YEAR_PRESETS)
@handle_errors
def apply_year(message):
    value = YEAR_PRESETS[message.text]
    filters = get_user_settings(message.chat.id)
    filters["year"] = value or ""
    save_user_settings(message.chat.id, filters)
    bot.send_message(
        message.chat.id,
        f"Ок, год: {message.text}",
        reply_markup=filters_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == TOGGLE_MOVIES_ONLY)
@handle_errors
def toggle_movies_only(message):
    filters = get_user_settings(message.chat.id)
    filters["movies_only"] = not filters.get("movies_only", False)
    save_user_settings(message.chat.id, filters)
    state = "включено ✅" if filters["movies_only"] else "выключено ❌"
    bot.send_message(
        message.chat.id,
        f"«Только фильмы» {state}",
        reply_markup=filters_keyboard(),
    )


@bot.message_handler(func=lambda m: m.text == RESET_FILTERS_BUTTON)
@handle_errors
def reset_filters(message):
    clear_user_settings(message.chat.id)
    bot.send_message(
        message.chat.id, "Фильтры сброшены.", reply_markup=filters_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == BACK_TO_FILTERS_BUTTON)
@handle_errors
def back_to_filters(message):
    show_filters(message)


# по настроению подбор


def matches_filters(movie, params, filters):
    """Кэш проходит те же ограничения, что и выдача API."""
    if not movie.get("id") or is_bad_movie(movie):
        return False
    if filters.get("movies_only") and movie.get("typeNumber") != 1:
        return False
    genres = {g.get("name") for g in (movie.get("genres") or [])}
    if not genres.intersection(params["genres"]):
        return False
    rating_range = filters.get("rating") or params["rating"]
    year_range = filters.get("year", params.get("year"))
    for value, bounds in (
        ((movie.get("rating") or {}).get("kp"), rating_range),
        (movie.get("year"), year_range),
    ):
        if bounds:
            try:
                low, high = map(float, bounds.split("-"))
                if not low <= float(value) <= high:
                    return False
            except (TypeError, ValueError):
                return False
    country = params.get("country")
    if country:
        countries = {c.get("name") for c in (movie.get("countries") or [])}
        if country.startswith("!"):
            if not countries or country[1:] in countries:
                return False
        elif country not in countries:
            return False
    return True


@bot.message_handler(func=lambda m: m.text == MOOD_BUTTON)
@handle_errors
def ask_mood(message):
    if not ZEXKORA_TOKEN:
        bot.send_message(
            message.chat.id,
            "Для подбора по настроению добавь ZEXKORA_TOKEN в .env. "
            "Пока можно выбрать жанр.",
            reply_markup=genres_keyboard(),
        )
        return
    bot.send_message(
        message.chat.id,
        "Опиши словами, что хочется посмотреть. Например:\n"
        "«хочу что-то тёплое, но не приторное»\n"
        "«отключить мозг после работы»\n"
        "«чтобы поплакать в конце»",
    )
    bot.register_next_step_handler(message, process_mood)


def get_candidates(chat_id: int, params: dict, filters: dict, limit: int = 15):
    type_number = 1 if filters.get("movies_only") else None
    year = filters.get("year", params.get("year"))
    country = params.get("country")

    movies = find_movies(
        params["genres"],
        rating=filters.get("rating") or params["rating"],
        year=year,
        country=country,
        type_number=type_number,
        limit=limit,
    )

    if movies:
        try:
            save_movies_to_cache(movies)
        except sqlite3.Error as e:
            logging.warning("Кэш недоступен: %s", type(e).__name__)

    keywords = params.get("keywords", [])
    if keywords:
        try:
            cached_movies = search_movies_by_keywords(keywords, limit=20)
            logging.debug("Кэш: найдено %s кандидатов", len(cached_movies))
            seen_ids = {m.get("id") for m in movies}
            for m in cached_movies:
                if m.get("id") not in seen_ids:
                    movies.append(m)
                    seen_ids.add(m.get("id"))
        except sqlite3.Error as e:
            logging.warning("Поиск в кэше недоступен: %s", type(e).__name__)

    shown = user_shown_ids.get(chat_id, set())
    return [
        m
        for m in movies
        if m.get("id") not in shown and matches_filters(m, params, filters)
    ]


@handle_errors
def process_mood(message):
    if dispatch_navigation(message):
        return
    if not message.text or not message.text.strip() or len(message.text) > 500:
        bot.send_message(
            message.chat.id, "Опиши настроение текстом до 500 символов."
        )
        bot.register_next_step_handler(message, process_mood)
        return
    user_text = message.text.strip()
    chat_id = message.chat.id

    if user_last_mood.get(chat_id) != user_text:
        user_shown_ids[chat_id] = set()

    user_last_mood[chat_id] = user_text

    bot.send_message(chat_id, "Думаю... 🤔")

    params = mood_to_params(user_text)
    if not params:
        bot.send_message(
            chat_id,
            "Не смог разобрать настроение. "
            "Попробуй переформулировать или выбери жанр вручную.",
            reply_markup=genres_keyboard(),
        )
        return

    filters = get_user_settings(chat_id)

    movies = get_candidates(chat_id, params, filters, limit=15)
    if not movies:
        bot.send_message(
            chat_id,
            "Больше ничего не нашлось по такому настроению. "
            "Попробуй другое настроение.",
            reply_markup=genres_keyboard(),
        )
        return

    bot.send_message(chat_id, "Выбираю лучший вариант... 🤔")
    result = pick_best_movie(movies, user_text, strict=True)

    if result == "no_match":
        print("Строгий поиск не дал результата, дозагружаю")
        bot.send_message(chat_id, "Ищу ещё варианты... 🤔")
        more = get_candidates(chat_id, params, filters, limit=40)
        seen_ids = {m.get("id") for m in movies}
        for m in more:
            if m.get("id") not in seen_ids:
                movies.append(m)
                seen_ids.add(m.get("id"))
        print(f"Всего кандидатов: {len(movies)}")
        result = pick_best_movie(movies, user_text, strict=False)

    if not result or result == "no_match":
        bot.send_message(
            chat_id,
            "Не нашёл подходящего фильма под такое настроение 🤔\n"
            "Попробуй переформулировать или описать иначе.",
            reply_markup=genres_keyboard(),
        )
        return

    movie, reason = result
    movie_id = movie.get("id")
    if movie_id:
        user_shown_ids.setdefault(chat_id, set()).add(movie_id)

    user_last_movie[chat_id] = movie
    add_history(chat_id, movie, query_type="mood", query_text=user_text)

    send_movie_card(
        chat_id, movie, has_mood=True, reason=reason, mood_text=user_text
    )
    user_last_genre[chat_id] = params["genres"][0]


@bot.message_handler(func=lambda m: m.text == MORE_MOOD_BUTTON)
@handle_errors
def handle_more_mood(message):
    mood = user_last_mood.get(message.chat.id)
    if not mood:
        bot.send_message(
            message.chat.id,
            "Сначала опиши настроение:",
            reply_markup=genres_keyboard(),
        )
        return
    message.text = mood
    process_mood(message)


# оценка


@bot.message_handler(func=lambda m: m.text == RATE_BUTTON)
@handle_errors
def ask_rating(message):
    movie = user_last_movie.get(message.chat.id)
    if not movie:
        bot.send_message(
            message.chat.id, "Сначала найди фильм, потом оценивай."
        )
        return
    name = movie.get("name") or movie.get("alternativeName") or "фильм"
    bot.send_message(
        message.chat.id,
        f"Оцени «{name}» от 1 до 10:",
        reply_markup=rate_keyboard(),
    )
    bot.register_next_step_handler(message, save_rating)


@handle_errors
def save_rating(message):
    if dispatch_navigation(message):
        return
    try:
        rating = int(message.text)
        if not 1 <= rating <= 10:
            raise ValueError
    except (ValueError, TypeError):
        bot.send_message(
            message.chat.id,
            "Введи число от 1 до 10.",
            reply_markup=rate_keyboard(),
        )
        bot.register_next_step_handler(message, save_rating)
        return
    movie = user_last_movie.get(message.chat.id)
    if not movie:
        bot.send_message(message.chat.id, "Сначала найди фильм.")
        return
    add_rating(message.chat.id, movie, rating)
    name = movie.get("name") or movie.get("alternativeName") or "фильм"
    bot.send_message(
        message.chat.id,
        f"✅ Оценка {rating}/10 для «{name}» сохранена!",
        reply_markup=after_movie_keyboard(
            has_mood=bool(user_last_mood.get(message.chat.id))
        ),
    )


# избранное и история


def send_text_pages(chat_id: int, text: str):
    """Делит длинную историю на сообщения с запасом для символов UTF-16."""
    pages = [text[i : i + 1800] for i in range(0, len(text), 1800)]
    for page in pages:
        bot.send_message(chat_id, page, reply_markup=genres_keyboard())


@bot.message_handler(func=lambda m: m.text == FAV_BUTTON)
@handle_errors
def add_to_favorites(message):
    movie = user_last_movie.get(message.chat.id)
    if not movie:
        bot.send_message(message.chat.id, "Сначала найди фильм.")
        return
    name = movie.get("name") or movie.get("alternativeName") or "фильм"
    if add_favorite(message.chat.id, movie):
        bot.send_message(message.chat.id, f"⭐ «{name}» добавлен в избранное!")
    else:
        bot.send_message(message.chat.id, f"«{name}» уже в избранном.")


@bot.message_handler(func=lambda m: m.text == HISTORY_BUTTON)
@handle_errors
def show_history(message):
    rows = get_history(message.chat.id, limit=10)
    if not rows:
        bot.send_message(
            message.chat.id,
            "📜 История пуста.",
            reply_markup=genres_keyboard(),
        )
        return
    text = "📜 Последние запросы:\n\n"
    for name, year, rating, qtype, qtext in rows:
        emoji = "💭" if qtype == "mood" else "🎭"
        rating_str = f"⭐ {rating}" if rating else "⭐ ?"
        text += f"{emoji} {name} ({year}) — {rating_str}\n"
        if qtext:
            if qtype == "mood":
                text += f"   «{qtext}»\n"
            else:
                text += f"   Жанр: {qtext}\n"
        text += "\n"
    text += "Чтобы очистить историю: /clear_history"
    send_text_pages(message.chat.id, text)


@bot.message_handler(commands=["clear_history"])
@handle_errors
def clear_history_cmd(message):
    clear_history(message.chat.id)
    bot.send_message(
        message.chat.id, "🗑️ История очищена.", reply_markup=genres_keyboard()
    )


@bot.message_handler(func=lambda m: m.text == FAVORITES_BUTTON)
@handle_errors
def show_favorites(message):
    rows = get_favorites(message.chat.id, limit=20)
    if not rows:
        bot.send_message(
            message.chat.id,
            "⭐ Избранное пусто.",
            reply_markup=genres_keyboard(),
        )
        return
    text = "⭐ Избранное:\n\n"
    for name, year, rating in rows:
        rating_str = f"⭐ {rating}" if rating else "⭐ ?"
        text += f"🎬 {name} ({year}) — {rating_str}\n"
    text += "\nЧтобы очистить избранное: /clear_favorites"
    send_text_pages(message.chat.id, text)


@bot.message_handler(commands=["clear_favorites"])
@handle_errors
def clear_favorites_cmd(message):
    clear_favorites(message.chat.id)
    bot.send_message(
        message.chat.id, "🗑️ Избранное очищено.", reply_markup=genres_keyboard()
    )


# неизвестные сообщения и навигация при пошаговом вводе


def dispatch_navigation(message):
    handlers = {
        BACK_BUTTON: handle_back,
        FILTERS_BUTTON: show_filters,
        MOOD_BUTTON: ask_mood,
        HISTORY_BUTTON: show_history,
        FAVORITES_BUTTON: show_favorites,
        MORE_GENRE_BUTTON: handle_more_genre,
        MORE_MOOD_BUTTON: handle_more_mood,
        FAV_BUTTON: add_to_favorites,
        RATE_BUTTON: ask_rating,
        SET_RATING_BUTTON: set_rating,
        SET_YEAR_BUTTON: set_year,
        TOGGLE_MOVIES_ONLY: toggle_movies_only,
        RESET_FILTERS_BUTTON: reset_filters,
        BACK_TO_FILTERS_BUTTON: back_to_filters,
        "/start": start,
        "/clear_history": clear_history_cmd,
        "/clear_favorites": clear_favorites_cmd,
    }
    text = message.text or ""
    handler = handlers.get(text) or handlers.get(text.split("@", 1)[0])
    if text.lower() in GENRES:
        handler = handle_genre
    elif text in RATING_PRESETS:
        handler = apply_rating
    elif text in YEAR_PRESETS:
        handler = apply_year
    if handler:
        handler(message)
        return True
    return False


@bot.message_handler(
    func=lambda message: True,
    content_types=[
        "text",
        "photo",
        "sticker",
        "document",
        "voice",
        "video",
        "audio",
    ],
)
@handle_errors
def unknown_message(message):
    bot.send_message(
        message.chat.id,
        "Выбери жанр или нажми «Опиши настроение».",
        reply_markup=genres_keyboard(),
    )
