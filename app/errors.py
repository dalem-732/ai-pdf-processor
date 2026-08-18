"""Ошибки уровня приложения с готовым HTTP-кодом и человеческим текстом.

Каждая ошибка знает, каким кодом ответить и что написать пользователю: это
избавляет обработчики API от лестницы if-ов и гарантирует, что клиент никогда
не получит трейсбек вместо объяснения.
"""

from __future__ import annotations


class AppError(Exception):
    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


class UnsupportedFileError(AppError):
    status_code = 415
    code = "unsupported_file"


class FileTooLargeError(AppError):
    status_code = 413
    code = "file_too_large"


class UnreadablePdfError(AppError):
    """Файл не открывается ни одним парсером: повреждён, обрезан или зашифрован."""

    status_code = 422
    code = "unreadable_pdf"


class NoTextLayerError(AppError):
    """PDF без текстового слоя: скан, который нужно сначала прогнать через OCR."""

    status_code = 422
    code = "no_text_layer"


class ProviderNotConfiguredError(AppError):
    status_code = 503
    code = "provider_not_configured"


class ProviderUnavailableError(AppError):
    status_code = 502
    code = "provider_unavailable"
