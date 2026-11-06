"""Shared bounded extraction behavior, without network or real PDF parsing."""
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tradingagents.dataflows import pdf
from tradingagents.dataflows.errors import VendorUnavailableError


def extract(client):
    return pdf.download_pdf_pages(client, 'https://example.test/document.pdf', source='Test',
                                  max_bytes=100, max_pages=2, max_text_chars=20)


@pytest.mark.parametrize(('content', 'reader', 'error'), [
    (b'not a PDF', lambda _: None, 'non-PDF'),
    (b'%PDF fake', Mock(side_effect=ValueError('broken')), 'cannot be parsed'),
])
def test_invalid_documents_close_response_and_are_unavailable(monkeypatch, content, reader, error):
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=reader))
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [content]
    client = Mock()
    client.get.return_value = response
    with pytest.raises(VendorUnavailableError, match=error):
        extract(client)
    response.close.assert_called_once()


def test_download_time_limit_stops_before_parsing(monkeypatch):
    reader = Mock()
    monkeypatch.setitem(sys.modules, 'pypdf', SimpleNamespace(PdfReader=reader))
    ticks = iter([0, 61])
    monkeypatch.setattr(pdf.time, 'monotonic', lambda: next(ticks))
    response = Mock(status_code=200, headers={})
    response.iter_content.return_value = [b'%PDF fake']
    client = Mock()
    client.get.return_value = response
    with pytest.raises(VendorUnavailableError, match='download bounds'):
        extract(client)
    reader.assert_not_called()
    response.close.assert_called_once()
