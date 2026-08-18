"""Извлечение текста из PDF тендерной документации."""

from app.pdf.extract import ExtractedDocument, PdfExtractionError, extract_text

__all__ = ["ExtractedDocument", "PdfExtractionError", "extract_text"]
