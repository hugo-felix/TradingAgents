import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.book_text import BookTextLibrary
from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import DesktopStore


def epub(path, text='趋势确认 <b>volume</b>', extra='', href='chapter.xhtml', encoding='utf-8'):
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container><rootfiles><rootfile full-path="OEBPS/book.opf"/>'
                         '</rootfiles></container>')
        archive.writestr('OEBPS/book.opf',
                         '<package><manifest><item id="c" href="' + href + '" '
                         'media-type="application/xhtml+xml"/></manifest>'
                         '<spine><itemref idref="c"/></spine></package>')
        archive.writestr('OEBPS/chapter.xhtml', (extra +
                         '<html xmlns="http://www.w3.org/1999/xhtml"><body>' + text +
                         '<script>untrusted()</script><style>hidden style</style></body></html>'
                         ).encode(encoding))
    return path


@pytest.fixture
def setup(tmp_path):
    store = DesktopStore(tmp_path / 'workspace')
    metadata = {'title': '测试书', 'author': '作者', 'translator': '无', 'language': '中文',
                'edition': '离线测试版', 'source': '合成样本', 'usage_scope': '测试'}
    def register(path):
        return BookRegistry(store).register(path, metadata)['book']
    return store, register, BookTextLibrary(store)


def test_epub_citation_search_and_restart(tmp_path, setup):
    store, register, library = setup
    book = register(epub(tmp_path / 'test.epub'))
    parsed = library.parse(book['book_id'])
    unit = parsed['units'][0]
    assert unit['text'] == '趋势确认 volume'
    assert unit['printed_page'] is None
    assert unit['member'] == 'OEBPS/chapter.xhtml'
    assert parsed['metadata'] == book['metadata']
    assert parsed['status'] == 'text_only'
    assert parsed == library.parse(book['book_id'])
    restored = BookTextLibrary(DesktopStore(store.directory))
    assert restored.get(parsed['parse_id']) == parsed
    assert len(restored.history(book['book_id'])) == 1
    assert 'units' not in restored.history(book['book_id'])[0]
    result = restored.search(parsed['parse_id'], '趋势确认 VOLUME')
    assert result['total'] == 1
    assert restored.search(parsed['parse_id'], '趋势 缺失')['total'] == 0
    citation = restored.citation(parsed['parse_id'], result['matches'][0]['ordinal'])
    assert citation['unit'] == unit
    assert citation['original_sha256'] == book['sha256']
    (library.books.originals / book['sha256']).unlink()
    assert restored.citation(parsed['parse_id'], 1)['original_status'] == 'missing'
    assert restored.search(parsed['parse_id'], 'volume')['total'] == 1
    with pytest.raises(ValueError, match='指纹'):
        restored.parse(book['book_id'])


def test_blank_pdf_is_partial_not_no_risk(tmp_path, setup):
    PdfWriter = pytest.importorskip('pypdf').PdfWriter
    _, register, library = setup
    path = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.write(path)
    parsed = library.parse(register(path)['book_id'])
    assert parsed['status'] == 'partial'
    assert parsed['units'] == [{'kind': 'pdf_file_page', 'ordinal': 1,
                                'printed_page': None, 'text': '', 'status': 'no_text'}]
    assert library.search(parsed['parse_id'], 'risk')['total'] == 0
    assert 'OCR' in parsed['limitations'][0]


def test_script_tail_preserved(tmp_path, setup):
    _, register, library = setup
    book = register(epub(tmp_path / 'test.epub', text='<script>ignore</script>真实条件'))
    parsed = library.parse(book['book_id'])
    assert parsed['units'][0]['text'] == '真实条件'


def test_missing_pdf_dependency_is_actionable(tmp_path, setup, monkeypatch):
    import sys
    _, register, library = setup
    source = tmp_path / 'test.pdf'
    source.write_bytes(b'%PDF-1.7\nfixture')
    book = register(source)
    monkeypatch.setitem(sys.modules, 'pypdf', None)
    with pytest.raises(ValueError, match='a-shares'):
        library.parse(book['book_id'])
    assert library.history(book['book_id']) == []


def test_epub_resource_limits_do_not_publish(tmp_path, setup, monkeypatch):
    import tradingagents.desktop.book_text as module
    _, register, library = setup
    book = register(epub(tmp_path / 'test.epub'))
    for key, limit in [('MAX_MEMBER', 10), ('MAX_TEXT', 1), ('MAX_UNITS', 0)]:
        with monkeypatch.context() as scoped:
            scoped.setattr(module, key, limit)
            with pytest.raises(ValueError):
                library.parse(book['book_id'])
            assert library.history(book['book_id']) == []


def test_utf16_entity_declaration_is_rejected(tmp_path, setup):
    _, register, library = setup
    source = epub(tmp_path / 'utf16.epub', text='&rule;', encoding='utf-16', extra=(
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE html [<!ENTITY rule "invented rule">]>'))
    book = register(source)
    with pytest.raises(ValueError, match='XML'):
        library.parse(book['book_id'])
    assert library.history(book['book_id']) == []


@pytest.mark.parametrize('href,extra', [
    ('https://example.com/book', ''), ('../../../escape', ''),
    ('chapter.xhtml', '<!DOCTYPE html [<!ENTITY x "expand">]>'),
])
def test_unsafe_or_unsupported_epub_does_not_publish(tmp_path, setup, href, extra):
    _, register, library = setup
    book = register(epub(tmp_path / 'test.epub', href=href, extra=extra))
    with pytest.raises(ValueError):
        library.parse(book['book_id'])
    assert library.history(book['book_id']) == []


def test_corrupt_pdf_and_original_change_never_publish(tmp_path, setup):
    _, register, library = setup
    path = tmp_path / 'broken.pdf'
    path.write_bytes(b'%PDF-1.7\ninvalid')
    book = register(path)
    with pytest.raises(ValueError):
        library.parse(book['book_id'])
    assert library.history(book['book_id']) == []
    (library.books.originals / book['sha256']).write_bytes(b'changed')
    with pytest.raises(ValueError, match='指纹'):
        library.parse(book['book_id'])


@pytest.mark.parametrize('ordinal', [0, -1, 2, True, '1'])
def test_invalid_location(tmp_path, setup, ordinal):
    _, register, library = setup
    parsed = library.parse(register(epub(tmp_path / 'test.epub'))['book_id'])
    with pytest.raises(ValueError):
        library.citation(parsed['parse_id'], ordinal)


@pytest.mark.parametrize('query', ['', ' ', '!!!', None, 'a' * 201])
def test_invalid_query(tmp_path, setup, query):
    _, register, library = setup
    parsed = library.parse(register(epub(tmp_path / 'test.epub'))['book_id'])
    with pytest.raises(ValueError):
        library.search(parsed['parse_id'], query)


def test_parser_upgrade_preserves_old_citation(tmp_path, setup, monkeypatch):
    import tradingagents.desktop.book_text as module
    _, register, library = setup
    book = register(epub(tmp_path / 'test.epub'))
    old = library.parse(book['book_id'])
    original = module._epub_units
    def changed(path):
        for unit in original(path):
            yield unit | {'text': unit['text'] + ' changed extraction'}
    monkeypatch.setattr(module, '_epub_units', changed)
    new = library.parse(book['book_id'])
    assert old['parse_id'] != new['parse_id']
    assert library.citation(old['parse_id'], 1)['unit']['text'] == '趋势确认 volume'
    assert len(library.history(book['book_id'])) == 2


def test_concurrent_identical_parses_and_api(tmp_path, setup):
    store, register, library = setup
    book = register(epub(tmp_path / 'test.epub'))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(library.parse, [book['book_id']] * 2))
    assert results[0] == results[1]
    api = DesktopAPI(store)
    parsed = api.parse_book_text(book['book_id'])['data']
    assert api.book_text_history(book['book_id'])['data'][0]['parse_id'] == parsed['parse_id']
    assert api.search_book_text(parsed['parse_id'], 'volume')['data']['total'] == 1
    assert api.book_text_citation(parsed['parse_id'], 1)['data']['unit']['ordinal'] == 1
    assert not api.book_text_citation('missing', 1)['ok']


def test_explicit_aliases_preserve_original_and_all_query_terms(tmp_path, setup):
    store, register, library = setup
    parsed = library.parse(register(epub(
        tmp_path / 'aliases.epub', text='Trading VOLUME and moving average.'))['book_id'])
    identifier = parsed['parse_id']
    aliases = {'成交量': ['volume', 'turnover'], '均线': ['moving average']}
    assert library.search(identifier, '成交量')['total'] == 0
    result = library.search(identifier, '成交量 均线', aliases)
    assert result['mode'] == 'explicit_aliases'
    assert result['total'] == 1
    assert result['matches'][0]['matched_terms'] == {
        '成交量': ['volume'], '均线': ['moving average']}
    assert result['term_groups']['成交量'] == ['turnover', 'volume', '成交量']
    assert library.search(identifier, '成交量 缺失', aliases)['total'] == 0
    # Expansion is one-way and never replaces original text or creates a parse version.
    assert library.search(identifier, '换手率', aliases)['total'] == 0
    assert library.get(identifier) == parsed
    assert len(library.history(parsed['book_id'])) == 1
    api = DesktopAPI(store)
    assert api.search_book_text(identifier, '成交量', aliases)['data']['total'] == 1
    assert not api.search_book_text(identifier, '成交量', {'成交量': 'volume'})['ok']


def test_aliases_are_literal_local_and_not_recursively_expanded(tmp_path, setup):
    _, register, library = setup
    parsed = library.parse(register(epub(
        tmp_path / 'aliases.epub', text='风险 trend a.b'))['book_id'])
    identifier = parsed['parse_id']
    assert library.search(identifier, '风险', {'风险': ['risk']})['total'] == 1
    result = library.search(identifier, '趋势', {'趋势': ['TREND', 'trend', ' trend ']})
    assert result['matches'][0]['matched_terms'] == {'趋势': ['trend']}
    assert library.search(identifier, '甲', {'甲': ['乙'], '乙': ['trend']})['total'] == 0
    assert library.search(identifier, '点', {'点': ['a.b']})['total'] == 1
    assert library.search(identifier, '点', {'点': ['a.*b']})['total'] == 0
    assert library.search(identifier, '趋势')['total'] == 0


def test_unicode_case_folding_preserves_excerpt_location(tmp_path, setup):
    _, register, library = setup
    text = 'ß' * 150 + ' VOLUME'
    parsed = library.parse(register(epub(tmp_path / 'unicode.epub', text=text))['book_id'])
    for query, aliases in [('volume', None), ('成交量', {'成交量': ['volume']})]:
        result = library.search(parsed['parse_id'], query, aliases)
        assert result['total'] == 1
        assert result['matches'][0]['excerpt'] == text[71:]


@pytest.mark.parametrize('aliases', [
    [], 'volume', {'': ['volume']}, {'two words': ['volume']},
    {'词': []}, {'词': ['v'] * 9}, {'词': 'volume'}, {'词': [None]},
    {'词': [' ']}, {'词': ['!!!']}, {'词': ['v' * 101]},
    {'A': ['a'], 'a': ['b']}, {str(i): ['word'] for i in range(33)},
])
def test_invalid_aliases_are_rejected(tmp_path, setup, aliases):
    _, register, library = setup
    parsed = library.parse(register(epub(tmp_path / 'test.epub'))['book_id'])
    with pytest.raises(ValueError):
        library.search(parsed['parse_id'], 'volume', aliases)
