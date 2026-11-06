
class MovieAPIError(RuntimeError):
    """API фильмов недоступен, отклонил запрос или вернул неверные данные."""


class MoodServiceError(RuntimeError):
    """Сервис подбора по настроению недоступен."""
