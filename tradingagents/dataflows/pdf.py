"""Bounded PDF transfer and text extraction; URL and cache policy live in callers."""
from __future__ import annotations

import io
import time

from tradingagents.dataflows.errors import VendorNotConfiguredError, VendorUnavailableError

PDF_TRUNCATION = '[PDF提取截断：达到页数、文字或时间上限]'


def download_pdf_pages(client, url, *, source, max_bytes, max_pages, max_text_chars):
    """Extract bounded physical pages from an already allowlisted URL.

    Downloads stop after 60 seconds; parsing stops between pages after 90 seconds
    total. A single synchronous parser/page call cannot be interrupted.
    """
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise VendorNotConfiguredError(
            f"{source} PDF extraction requires pypdf; pip install '.[a-shares]'"
        ) from exc
    started = time.monotonic()
    response = client.get(url, timeout=(10, 20), stream=True, allow_redirects=False)
    try:
        if 300 <= response.status_code < 400:
            raise VendorUnavailableError(f'{source} PDF redirects are not followed')
        response.raise_for_status()
        if int(response.headers.get('Content-Length', 0)) > max_bytes:
            raise VendorUnavailableError(f'{source} PDF exceeds byte limit')
        content = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            content.extend(chunk)
            if len(content) > max_bytes or time.monotonic() - started > 60:
                raise VendorUnavailableError(f'{source} PDF exceeds download bounds')
    finally:
        response.close()
    if not content.startswith(b'%PDF'):
        raise VendorUnavailableError(f'{source} returned a non-PDF document')
    try:
        reader = PdfReader(io.BytesIO(content))
        pages, size = [], 0
        truncated = len(reader.pages) > max_pages
        for page in reader.pages[:max_pages]:
            if time.monotonic() - started > 90:
                truncated = True
                break
            text = page.extract_text() or ''
            remaining = max_text_chars - size
            pages.append(text[:remaining])
            size += len(pages[-1])
            if len(text) > remaining or size >= max_text_chars:
                truncated = True
                break
        if truncated:
            pages.append(PDF_TRUNCATION)
    except Exception as exc:
        raise VendorUnavailableError(f'{source} PDF cannot be parsed') from exc
    return pages
