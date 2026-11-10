import json
import threading
import zipfile
from concurrent.futures import ThreadPoolExecutor

import pytest

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.book_checkpoints import BookCheckpoint
from tradingagents.desktop.book_jobs import BookTextJobs
from tradingagents.desktop.book_text import BookTextLibrary
from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import DesktopStore


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / 'book.epub'
    with zipfile.ZipFile(path, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container><rootfile full-path="book.opf"/></container>')
        archive.writestr('book.opf', '<package><manifest><item id="c" href="chapter.xhtml" '
                         'media-type="application/xhtml+xml"/></manifest>'
                         '<spine><itemref idref="c"/></spine></package>')
        archive.writestr('chapter.xhtml', '<html><body>Offline source text</body></html>')
    store = DesktopStore(tmp_path / 'workspace')
    metadata = {'title': 'Synthetic', 'author': 'Test', 'translator': 'None',
                'language': 'English', 'edition': 'Test edition', 'source': 'Synthetic',
                'usage_scope': 'Offline tests'}
    book = BookRegistry(store).register(path, metadata)['book']['book_id']
    jobs = BookTextJobs(store)
    yield store, book, jobs
    jobs.close()
    if jobs.thread:
        jobs.thread.join(timeout=5)
        assert not jobs.thread.is_alive()


def wait(jobs):
    jobs.thread.join(timeout=5)
    assert not jobs.thread.is_alive()


def paused(jobs, monkeypatch, *, after_extract=False):
    entered, release = threading.Event(), threading.Event()
    parse = jobs.library.parse

    def parse_paused(*args, **kwargs):
        if after_extract:
            result = parse(*args, **kwargs)
        entered.set()
        assert release.wait(5)
        return result if after_extract else parse(*args, **kwargs)

    monkeypatch.setattr(jobs.library, 'parse', parse_paused)
    return entered, release


def test_success_and_dedup_keep_original_timestamp(setup):
    store, book, jobs = setup
    store.setting('task', {'status': 'running', 'id': 'independent-analysis'})
    first = jobs.start(book)
    wait(jobs)
    result = jobs.status(book)
    assert first['id'] == result['id']
    assert result['status'] == 'completed'
    assert result['completed_units'] == 1
    original = jobs.library.get(result['parse_id'])
    assert original['units'][0]['text'] == 'Offline source text'
    jobs.start(book)
    wait(jobs)
    assert jobs.library.get(result['parse_id']) == original
    assert len(jobs.library.history(book)) == 1
    assert store.setting('task')['id'] == 'independent-analysis'


@pytest.mark.parametrize('after_extract', [False, True])
def test_cancellation_before_publication_never_publishes(setup, monkeypatch, after_extract):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch, after_extract=after_extract)
    try:
        job = jobs.start(book)
        assert entered.wait(5)
        assert jobs.cancel(job['id'])['status'] == 'cancelling'
        assert jobs.library.history(book) == []
    finally:
        release.set()
    wait(jobs)
    assert jobs.status(book)['status'] == 'cancelled'
    assert jobs.status(book)['parse_id'] is None
    assert jobs.library.history(book) == []
    assert jobs.cancel(job['id'])['status'] == 'cancelled'


def test_concurrent_starts_and_stale_cancel(setup, monkeypatch):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch)
    try:
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: jobs.start(book), range(8)))
        assert entered.wait(5)
        assert len({result['id'] for result in results}) == 1
        old = results[0]['id']
        jobs.cancel(old)
    finally:
        release.set()
    wait(jobs)
    jobs.start(book)
    wait(jobs)
    assert jobs.cancel(old)['status'] == 'cancelled'
    assert jobs.status(book)['status'] == 'completed'
    with pytest.raises(ValueError, match='未找到'):
        jobs.cancel('missing')


def test_failed_job_can_retry_and_old_version_survives(setup, monkeypatch):
    _, book, jobs = setup
    jobs.start(book)
    wait(jobs)
    original = jobs.library.history(book)
    with monkeypatch.context() as scoped:
        def fail(*args, **kwargs):
            kwargs['progress'](2)
            raise ValueError('sensitive path or document content')
        scoped.setattr(jobs.library, 'parse', fail)
        jobs.start(book)
        wait(jobs)
    failed = jobs.status(book)
    assert failed['status'] == 'failed'
    assert failed['completed_units'] == 2
    assert 'sensitive' not in failed['error']
    assert failed['parse_id'] is None
    assert jobs.library.history(book) == original
    jobs.start(book)
    wait(jobs)
    assert jobs.status(book)['status'] == 'completed'
    assert jobs.library.history(book) == original


@pytest.mark.parametrize('failure', ['success_record', 'checkpoint_cleanup'])
def test_publication_and_success_commit_atomically(setup, monkeypatch, failure):
    store, book, jobs = setup
    save = jobs._save

    def fail_success(job, db):
        if job['status'] == 'completed':
            raise OSError('storage failure')
        return save(job, db)

    if failure == 'success_record':
        monkeypatch.setattr(jobs, '_save', fail_success)
    else:
        def fail_cleanup(self, db):
            raise OSError('checkpoint cleanup failure')
        monkeypatch.setattr(BookCheckpoint, 'delete', fail_cleanup)
    jobs.start(book)
    wait(jobs)
    assert jobs.status(book)['status'] == 'failed'
    assert jobs.library.history(book) == []
    with store.connect() as db:
        assert BookCheckpoint(store, jobs.status(book)['id']).count(db) == 1


def test_restart_retains_progress_and_marks_interruption(setup):
    store, book, jobs = setup
    with store.connect() as db:
        jobs._save({'id': 'previous', 'book_id': book, 'status': 'running', 'completed_units': 7,
                    'parse_id': None, 'started_at': '2026-11-10T00:00:00+00:00'}, db)
    restored = BookTextJobs(DesktopStore(store.directory))
    status = restored.status(book)
    assert status['status'] == 'interrupted'
    assert status['completed_units'] == 7
    assert restored.library.history(book) == []
    restored.start(book)
    wait(restored)
    assert restored.status(book)['completed_units'] == 1
    restored.close()


def test_close_blocks_publication_and_new_jobs(setup, monkeypatch):
    _, book, jobs = setup
    entered, release = paused(jobs, monkeypatch, after_extract=True)
    try:
        jobs.start(book)
        assert entered.wait(5)
        jobs.close()
        with pytest.raises(ValueError, match='退出'):
            jobs.start(book)
    finally:
        release.set()
    wait(jobs)
    assert jobs.status(book)['status'] == 'cancelled'
    assert jobs.library.history(book) == []


def test_stage_without_publish_and_bridge(setup):
    store, book, jobs = setup
    counts = []
    staged = BookTextLibrary(store).parse(book, persist=False, progress=counts.append)
    assert counts == [0, 1]
    assert staged['units']
    assert jobs.library.history(book) == []
    api = DesktopAPI(store)
    try:
        assert api.book_text_status(book)['data']['status'] == 'idle'
        assert not api.start_book_text('missing')['ok']
        assert api.start_book_text(book)['ok']
        wait(api._book_jobs)
        assert api.book_text_status(book)['data']['status'] == 'completed'
    finally:
        api._book_jobs.close()


def checkpoint_after_cancel(jobs, book, monkeypatch):
    entered, release = paused(jobs, monkeypatch, after_extract=True)
    job = jobs.start(book)
    try:
        assert entered.wait(5)
        jobs.cancel(job['id'])
    finally:
        release.set()
    wait(jobs)
    return job['id']


def test_resume_skips_extracted_chapter_and_cleans_only_new_checkpoint(setup, monkeypatch):
    store, book, jobs = setup
    old = checkpoint_after_cancel(jobs, book, monkeypatch)
    assert jobs.status(book)['resumable_units'] == 1
    from tradingagents.desktop import book_text
    xml = book_text._xml

    def no_chapter_reextract(archive, name):
        assert name != 'chapter.xhtml'
        return xml(archive, name)

    monkeypatch.setattr(book_text, '_xml', no_chapter_reextract)
    api = DesktopAPI(store)
    try:
        resumed = api.resume_book_text(old)['data']
        wait(api._book_jobs)
        result = api._book_jobs.status(book)
        assert result['status'] == 'completed'
        assert result['id'] != old
        assert result['resumed_from'] == old
        assert result['completed_units'] == 1
        assert api._book_jobs.library.get(result['parse_id'])['units'][0]['text']
        with store.connect() as db:
            assert BookCheckpoint(store, old).count(db) == 1
            assert BookCheckpoint(store, resumed['id']).count(db) == 0
        assert not api.resume_book_text(old)['ok']
        assert not api.resume_book_text('missing')['ok']
        assert not api.resume_book_text(result['id'])['ok']
    finally:
        api._book_jobs.close()


@pytest.mark.parametrize('damage', ['parser', 'source', 'text', 'ordinal', 'missing_header'])
def test_invalid_checkpoint_never_publishes_and_fresh_retry_works(setup, monkeypatch, damage):
    store, book, jobs = setup
    old = checkpoint_after_cancel(jobs, book, monkeypatch)
    with store.connect() as db:
        if damage in {'parser', 'source'}:
            binding = json.loads(db.execute('SELECT binding FROM book_checkpoint_headers '
                                            'WHERE job_id=?', (old,)).fetchone()[0])
            binding[2 if damage == 'parser' else 1] = 'changed'
            db.execute('UPDATE book_checkpoint_headers SET binding=? WHERE job_id=?',
                       (json.dumps(binding), old))
        elif damage == 'missing_header':
            db.execute('DELETE FROM book_checkpoint_headers WHERE job_id=?', (old,))
        elif damage == 'text':
            unit = json.loads(db.execute('SELECT document FROM book_checkpoint_units '
                                         'WHERE job_id=?', (old,)).fetchone()[0])
            unit['text'] = 'Changed checkpoint text'
            db.execute('UPDATE book_checkpoint_units SET document=? WHERE job_id=?',
                       (json.dumps(unit), old))
        else:
            db.execute('UPDATE book_checkpoint_units SET ordinal=2 WHERE job_id=?', (old,))
    jobs.resume(old)
    wait(jobs)
    assert jobs.status(book)['status'] == 'failed'
    assert jobs.library.history(book) == []
    jobs.start(book)
    wait(jobs)
    assert jobs.status(book)['status'] == 'completed'


def test_restart_resume_and_duplicate_request_keep_exact_identity(setup, monkeypatch):
    store, book, jobs = setup
    old = checkpoint_after_cancel(jobs, book, monkeypatch)
    with store.connect() as db:
        row = db.execute('SELECT document FROM book_text_jobs WHERE id=?', (old,)).fetchone()
        interrupted = json.loads(row[0]) | {'status': 'running'}
        jobs._save(interrupted, db)
    restored = BookTextJobs(store)
    entered, release = paused(restored, monkeypatch)
    try:
        assert restored.status(book)['status'] == 'interrupted'
        assert restored.status(book)['resumable_units'] == 1
        first = restored.resume(old)
        assert entered.wait(5)
        assert restored.resume(old)['id'] == first['id']
        assert restored.cancel(old)['status'] == 'interrupted'
        assert restored.status(book)['status'] == 'running'
    finally:
        release.set()
        wait(restored)
        restored.close()
    assert restored.status(book)['status'] == 'completed'


def test_partial_epub_resume_skips_only_completed_units(setup, monkeypatch, tmp_path):
    _, _, jobs = setup
    source = tmp_path / 'three.epub'
    with zipfile.ZipFile(source, 'w') as archive:
        archive.writestr('mimetype', 'application/epub+zip')
        archive.writestr('META-INF/container.xml',
                         '<container><rootfile full-path="book.opf"/></container>')
        archive.writestr('book.opf', '<package><manifest>' + ''.join(
            f'<item id="c{n}" href="c{n}.xhtml" media-type="application/xhtml+xml"/>'
            for n in range(1, 4)) + '</manifest><spine>' + ''.join(
            f'<itemref idref="c{n}"/>' for n in range(1, 4)) + '</spine></package>')
        for n in range(1, 4):
            archive.writestr(f'c{n}.xhtml', f'<html><body>Chapter {n}</body></html>')
    metadata = jobs.library.books.list_books()[0]['metadata']
    book = jobs.library.books.register(source, metadata)['book']['book_id']
    progress = jobs._progress

    def cancel_after_first(job, count):
        if count == 1:
            jobs.cancel(job['id'])
        progress(job, count)

    monkeypatch.setattr(jobs, '_progress', cancel_after_first)
    old = jobs.start(book)['id']
    wait(jobs)
    assert jobs.status(book)['resumable_units'] == 1
    monkeypatch.setattr(jobs, '_progress', progress)
    from tradingagents.desktop import book_text
    xml, visited = book_text._xml, []

    def track(archive, name):
        visited.append(name)
        return xml(archive, name)

    monkeypatch.setattr(book_text, '_xml', track)
    jobs.resume(old)
    wait(jobs)
    result = jobs.status(book)
    assert result['status'] == 'completed'
    assert result['completed_units'] == 3
    assert 'c1.xhtml' not in visited
    assert 'c2.xhtml' in visited and 'c3.xhtml' in visited
    resumed = jobs.library.get(result['parse_id'])
    assert [unit['ordinal'] for unit in resumed['units']] == [1, 2, 3]
    assert jobs.library.parse(book)['parse_id'] == resumed['parse_id']


def test_pdf_resume_does_not_extract_completed_pages(setup, monkeypatch, tmp_path):
    pypdf = pytest.importorskip('pypdf')
    from pypdf._page import PageObject
    _, original_book, jobs = setup
    source = tmp_path / 'three.pdf'
    writer = pypdf.PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=100, height=100)
    writer.write(source)
    book = jobs.library.books.register(
        source, jobs.library.books.get(original_book)['metadata'])['book']['book_id']
    calls = []
    monkeypatch.setattr(PageObject, 'extract_text', lambda page: calls.append(page) or 'Text')
    progress = jobs._progress

    def cancel_first(job, count):
        if count == 1:
            jobs.cancel(job['id'])
        progress(job, count)

    monkeypatch.setattr(jobs, '_progress', cancel_first)
    old = jobs.start(book)['id']
    wait(jobs)
    assert len(calls) == 1
    monkeypatch.setattr(jobs, '_progress', progress)
    jobs.resume(old)
    wait(jobs)
    assert jobs.status(book)['status'] == 'completed'
    assert len(calls) == 3
    assert len(jobs.library.get(jobs.status(book)['parse_id'])['units']) == 3


@pytest.mark.parametrize('limit', ['MAX_UNITS', 'MAX_TEXT'])
def test_resume_rechecks_local_limits(setup, monkeypatch, limit):
    _, book, jobs = setup
    old = checkpoint_after_cancel(jobs, book, monkeypatch)
    monkeypatch.setattr('tradingagents.desktop.book_text.' + limit, 0)
    jobs.resume(old)
    wait(jobs)
    assert jobs.status(book)['status'] == 'failed'
    assert jobs.library.history(book) == []


def test_resume_requires_intact_original(setup, monkeypatch):
    _, book, jobs = setup
    old = checkpoint_after_cancel(jobs, book, monkeypatch)
    original = jobs.library.books.originals / jobs.library.books.get(book)['sha256']
    original.write_bytes(b'changed')
    jobs.resume(old)
    wait(jobs)
    assert jobs.status(book)['status'] == 'failed'
    assert jobs.library.history(book) == []
