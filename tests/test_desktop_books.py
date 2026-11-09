import copy
import hashlib
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import DesktopStore


@pytest.fixture
def metadata():
    return {'title': '测试书籍', 'author': '测试作者', 'translator': '无',
            'language': '中文', 'edition': '测试第一版', 'source': '合成离线样本',
            'usage_scope': '仅用于回归测试'}


def pdf(path, content=b'%PDF-1.7\nsynthetic fixture'):
    path.write_bytes(content)
    return path


def test_registration_preserves_original_and_deduplicates_after_restart(tmp_path, metadata):
    source = pdf(tmp_path / 'source.pdf')
    store = DesktopStore(tmp_path / 'workspace')
    registry = BookRegistry(store)
    first = registry.register(source, metadata)
    book = first['book']
    assert not first['duplicate']
    assert book['sha256'] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert book['size_bytes'] == source.stat().st_size
    assert book['parsing'] == book['indexing'] == 'not_started'
    metadata['title'] = 'caller mutation'
    renamed = tmp_path / 'renamed.pdf'
    renamed.write_bytes(source.read_bytes())
    registry = BookRegistry(DesktopStore(store.directory))
    assert registry.register(renamed, book['metadata']) == {'duplicate': True, 'book': book}
    source.unlink()
    assert registry.verify(book['book_id'])['original_status'] == 'available'
    assert registry.get(book['book_id']) == book
    assert registry.list_books() == [book]
    assert len(list(registry.originals.iterdir())) == 1


def test_editions_and_translations_keep_separate_identity(tmp_path, metadata):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    source = pdf(tmp_path / 'source.pdf')
    first = registry.register(source, metadata)['book']
    revised = dict(metadata, edition='第二版', language='英文', translator='另一译者')
    with pytest.raises(ValueError, match='相同文件'):
        registry.register(source, revised)
    pdf(source, b'%PDF-1.7\nrevised edition')
    second = registry.register(source, revised)['book']
    assert first['book_id'] != second['book_id']
    assert first['sha256'] != second['sha256']
    assert registry.get(first['book_id']) == first
    assert len(registry.list_books()) == len(list(registry.originals.iterdir())) == 2


def test_integrity_status_does_not_rewrite_registration(tmp_path, metadata):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    book = registry.register(pdf(tmp_path / 'source.pdf'), metadata)['book']
    original = registry.originals / book['sha256']
    original.write_bytes(b'corrupt')
    assert registry.verify(book['book_id'])['original_status'] == 'changed'
    original.unlink()
    assert registry.verify(book['book_id'])['original_status'] == 'missing'
    assert registry.get(book['book_id']) == book
    with pytest.raises(ValueError):
        registry.verify('unknown')


@pytest.mark.parametrize('suffix, content', [
    ('.pdf', b'not a PDF'), ('.epub', b'not a zip'), ('.txt', b'%PDF-1.7'),
])
def test_invalid_files_leave_no_registration_or_temporary_files(tmp_path, metadata, suffix, content):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    source = tmp_path / ('source' + suffix)
    source.write_bytes(content)
    with pytest.raises(ValueError):
        registry.register(source, metadata)
    assert registry.list_books() == []
    assert not registry.originals.exists() or list(registry.originals.iterdir()) == []


def test_epub_registration_does_not_extract_archive(tmp_path, metadata):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    source = tmp_path / 'book.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('../never-extract.txt', 'untrusted content')
    book = registry.register(source, metadata)['book']
    assert book['format'] == 'epub'
    assert not (tmp_path / 'never-extract.txt').exists()
    assert registry.verify(book['book_id'])['original_status'] == 'available'


@pytest.mark.parametrize('mimetype', [None, 'text/plain'])
def test_epub_requires_its_mimetype(tmp_path, metadata, mimetype):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    source = tmp_path / 'invalid.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        if mimetype is not None:
            archive.writestr('mimetype', mimetype)
    with pytest.raises(ValueError):
        registry.register(source, metadata)
    assert registry.list_books() == []
    assert list(registry.originals.iterdir()) == []


def test_concurrent_duplicate_registration_has_one_identity(tmp_path, metadata):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    source = pdf(tmp_path / 'source.pdf')
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(registry.register, source, metadata) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(row['duplicate'] for row in results) == [False, True]
    assert results[0]['book'] == results[1]['book']
    assert registry.list_books() == [results[0]['book']]
    assert len(list(registry.originals.iterdir())) == 1


def test_metadata_validation_precedes_filesystem_access(tmp_path, metadata):
    registry = BookRegistry(DesktopStore(tmp_path / 'workspace'))
    for field in metadata:
        invalid = copy.deepcopy(metadata)
        invalid[field] = ''
        with pytest.raises(ValueError):
            registry.register(tmp_path / 'missing.pdf', invalid)
    assert not registry.originals.exists()
    assert registry.list_books() == []


def test_bridge_uses_native_picker_and_handles_cancellation(tmp_path, metadata, monkeypatch):
    monkeypatch.setitem(sys.modules, 'webview', SimpleNamespace(
        FileDialog=SimpleNamespace(OPEN='open')))
    api = DesktopAPI(DesktopStore(tmp_path / 'workspace'))
    assert not api.register_book(metadata)['ok']
    assert api.list_books()['data'] == []
    api._window = SimpleNamespace(create_file_dialog=lambda *a, **k: None)
    assert api.register_book(metadata) == {'ok': True, 'data': {'cancelled': True}}
    source = pdf(tmp_path / 'selected.pdf')
    calls = []

    def choose(*args, **kwargs):
        calls.append((args, kwargs))
        return (str(source),)

    api._window = SimpleNamespace(create_file_dialog=choose)
    result = api.register_book(metadata)
    assert result['ok']
    book = result['data']['book']
    assert calls[0][1]['allow_multiple'] is False
    assert api.list_books()['data'] == [book]
    assert api.get_book(book['book_id'])['data'] == book
    assert api.verify_book(book['book_id'])['data']['original_status'] == 'available'
    assert not api.get_book('missing')['ok']
