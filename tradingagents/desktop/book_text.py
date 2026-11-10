"""Offline text extraction and immutable, edition-bound source locations.

This deliberately does not interpret images, formulas or document instructions.
"""

from __future__ import annotations

import hashlib
import io
import json
import posixpath
import re
import zipfile
from importlib.metadata import version
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import now

MAX_MEMBER = 8 * 1024 * 1024
MAX_TEXT = 32 * 1024 * 1024
MAX_UNITS = 10000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class _BookXML(ET.TreeBuilder):
    def doctype(self, name, pubid, system):
        # The parser callback also rejects declarations in UTF-16 XML.
        raise ValueError('EPUB 含不支持的 XML 实体声明。')


def _xml(archive, name):
    info = archive.getinfo(name)
    if info.file_size > MAX_MEMBER:
        raise ValueError('EPUB 文档过大，未解析。')
    content = archive.read(info)
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise ValueError('EPUB 含不支持的 XML 实体声明。')
    return ET.fromstring(content, parser=ET.XMLParser(target=_BookXML()))


def _epub_units(path, start_after=0):
    with zipfile.ZipFile(path) as archive:
        root = _xml(archive, 'META-INF/container.xml')
        package = root.find('.//{*}rootfile')
        if package is None:
            raise ValueError('EPUB 缺少内容目录。')
        package_path = package.attrib['full-path']
        document = _xml(archive, package_path)
        manifest = {item.attrib['id']: item for item in document.findall('./{*}manifest/{*}item')}
        spine = document.findall('./{*}spine/{*}itemref')
        if not spine or len(spine) > MAX_UNITS or start_after > len(spine):
            raise ValueError('EPUB 阅读顺序缺失或过大。')
        for ordinal, ref in enumerate(spine, 1):
            item = manifest[ref.attrib['idref']]
            if item.attrib.get('media-type') != 'application/xhtml+xml':
                raise ValueError('EPUB 含不支持的章节格式。')
            href = urlsplit(item.attrib['href'])
            if href.scheme or href.netloc:
                raise ValueError('EPUB 章节必须位于本地书籍中。')
            member = posixpath.normpath(posixpath.join(
                posixpath.dirname(package_path), unquote(href.path)))
            if member.startswith(('../', '/')) or '\\' in member:
                raise ValueError('EPUB 章节路径无效。')
            if ordinal <= start_after:
                continue
            chapter = _xml(archive, member)
            # Never execute or render markup, scripts, styles, or embedded content.
            for parent in chapter.iter():
                for child in list(parent):
                    if child.tag.rsplit('}', 1)[-1] in {'script', 'style'}:
                        tail = child.tail
                        child.clear()
                        child.tail = tail
            body = chapter.find('.//{*}body')
            text = ' '.join(' '.join(body.itertext()).split()) if body is not None else ''
            yield {'kind': 'epub_spine', 'ordinal': ordinal, 'member': member,
                   'printed_page': None, 'text': text}


class BookTextLibrary:
    def __init__(self, store):
        self.store = store
        self.books = BookRegistry(store)
        with store.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS book_parses (
                parse_id TEXT PRIMARY KEY, book_id TEXT NOT NULL, document TEXT NOT NULL)''')

    def parse(self, book_id, *, progress=None, persist=True, checkpoint=None):
        # Background callers may stage extraction without publishing a version.
        # Cancellation is cooperative at these boundaries, never partial success.
        if progress:
            progress(0)
        book = self.books.get(book_id)
        if self.books.verify(book_id)['original_status'] != 'available':
            raise ValueError('原文快照缺失、不可读或指纹不符，不能解析。')
        path = self.books.originals / book['sha256']
        with path.open('rb') as source:
            content = source.read(256 * 1024 * 1024 + 1)
        if len(content) > 256 * 1024 * 1024:
            raise ValueError('原文件超过本地文本解析的 256 MiB 上限。')
        if hashlib.sha256(content).hexdigest() != book['sha256']:
            raise ValueError('原文在读取时发生变更，不能解析。')
        snapshot = io.BytesIO(content)
        parser = 'stockseek-epub-text/1'
        try:
            if book['format'] == 'pdf':
                try:
                    from pypdf import PdfReader
                except ImportError as error:
                    raise ValueError('PDF 文本解析需要安装项目 a-shares 可选依赖。') from error
                parser = f'stockseek-pdf-text/1;pypdf/{version("pypdf")}'
                reader = PdfReader(snapshot)
                if reader.is_encrypted:
                    raise ValueError('暂不支持加密 PDF。')
                if len(reader.pages) > MAX_UNITS:
                    raise ValueError('PDF 页数超过本地解析上限。')
                extracted = checkpoint.load(book_id, book['sha256'], parser) if checkpoint else []
                if len(extracted) > len(reader.pages):
                    raise ValueError('Checkpoint exceeds PDF page count')
                start_after = len(extracted)
                units = ({'kind': 'pdf_file_page', 'ordinal': number, 'printed_page': None,
                          'text': page.extract_text() or ''}
                         for number, page in enumerate(reader.pages, 1) if number > start_after)
            else:
                extracted = checkpoint.load(book_id, book['sha256'], parser) if checkpoint else []
                units = (_epub_units(snapshot, len(extracted)) if extracted
                         else _epub_units(snapshot))
            size = 0
            for ordinal, unit in enumerate(extracted, 1):
                expected_kind = 'pdf_file_page' if book['format'] == 'pdf' else 'epub_spine'
                if (unit.get('kind') != expected_kind or unit.get('ordinal') != ordinal
                        or not isinstance(unit.get('text'), str)
                        or unit.get('printed_page') is not None
                        or unit.get('status') != ('text_extracted' if unit['text'] else 'no_text')):
                    raise ValueError('Invalid extraction checkpoint unit')
                size += len(unit['text'].encode('utf-8'))
            if len(extracted) > MAX_UNITS or size > MAX_TEXT:
                raise ValueError('Extraction checkpoint exceeds local limits')
            if progress and extracted:
                progress(len(extracted))
            for unit in units:
                size += len(unit['text'].encode('utf-8'))
                if size > MAX_TEXT:
                    raise ValueError('提取文本超过本地解析上限。')
                unit['text'] = unit['text'].strip()
                unit['status'] = 'text_extracted' if unit['text'] else 'no_text'
                if checkpoint:
                    checkpoint.append(unit)
                extracted.append(unit)
                if progress:
                    progress(len(extracted))
            if not extracted:
                raise ValueError('文档没有可定位的页面或章节。')
        except ValueError:
            raise
        except Exception as error:
            raise ValueError('书籍文本解析失败；原文和已有解析版本均保留。') from error
        payload = {'book_id': book_id, 'original_sha256': book['sha256'],
                   'metadata': book['metadata'], 'parser': parser, 'units': extracted,
                   'status': 'partial' if any(not u['text'] for u in extracted) else 'text_only',
                   'limitations': ['未执行 OCR、图表、公式或版面关系识别；文本顺序需核对原书。',
                                   'PDF 使用文件页，印刷页未知；EPUB 使用阅读顺序章节，无页码。']}
        parse_id = hashlib.sha256(_json(payload).encode('utf-8')).hexdigest()
        document = dict(payload, parse_id=parse_id, created_at=now())
        if not persist:
            return document
        with self.store.connect() as db:
            return self.publish(document, db)

    def publish(self, document, db):
        """Publish staged extraction in the caller's transaction (internal use)."""
        db.execute('INSERT OR IGNORE INTO book_parses VALUES (?, ?, ?)',
                   (document['parse_id'], document['book_id'], _json(document)))
        row = db.execute('SELECT document FROM book_parses WHERE parse_id=?',
                         (document['parse_id'],)).fetchone()
        return json.loads(row[0])

    def get(self, parse_id):
        with self.store.connect() as db:
            row = db.execute('SELECT document FROM book_parses WHERE parse_id=?',
                             (parse_id,)).fetchone()
        if row is None:
            raise ValueError('未找到该解析版本。')
        return json.loads(row[0])

    def history(self, book_id):
        self.books.get(book_id)
        with self.store.connect() as db:
            rows = db.execute('SELECT document FROM book_parses WHERE book_id=? '
                              'ORDER BY rowid DESC', (book_id,)).fetchall()
        return [{key: value for key, value in json.loads(row[0]).items() if key != 'units'}
                for row in rows]

    def citation(self, parse_id, ordinal):
        document = self.get(parse_id)
        if type(ordinal) is not int or not 1 <= ordinal <= len(document['units']):
            raise ValueError('原文位置无效。')
        return {key: value for key, value in document.items() if key != 'units'} | {
            'unit': document['units'][ordinal - 1],
            'original_status': self.books.verify(document['book_id'])['original_status']}

    def search(self, parse_id, query):
        if not isinstance(query, str) or not query.strip() or len(query) > 200:
            raise ValueError('请输入 1–200 字的原文关键词。')
        terms = set(re.findall(r'\w+', query.casefold()))
        if not terms:
            raise ValueError('请输入文字或数字关键词。')
        document = self.get(parse_id)
        matches = []
        for unit in document['units']:
            folded = unit['text'].casefold()
            if all(term in folded for term in terms):
                position = min(folded.index(term) for term in terms)
                # Full original text remains available through citation(), without an LLM.
                matches.append({'parse_id': parse_id, 'ordinal': unit['ordinal'],
                                'kind': unit['kind'],
                                'excerpt': unit['text'][max(0, position - 80):position + 240]})
        return {'mode': 'literal_all_terms', 'query': query.strip(), 'parse_id': parse_id,
                'total': len(matches), 'matches': matches[:50],
                'limitations': '仅检索指定解析版本的原文关键词；无语义召回、翻译或答案推断。'}
