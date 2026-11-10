"""Offline browser acceptance of cancellable local extraction through the real API."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
import zipfile
from contextlib import ExitStack
from pathlib import Path

from playwright.sync_api import sync_playwright

from tradingagents.desktop.api import DesktopAPI
from tradingagents.desktop.books import BookRegistry
from tradingagents.desktop.store import DesktopStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--chromium-path')
    options = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory, ExitStack() as cleanup:
        root = Path(directory)
        source = root / 'synthetic.epub'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml',
                             '<container><rootfile full-path="book.opf"/></container>')
            archive.writestr('book.opf', '<package><manifest><item id="c" href="c.xhtml" '
                             'media-type="application/xhtml+xml"/></manifest>'
                             '<spine><itemref idref="c"/></spine></package>')
            archive.writestr('c.xhtml', '<html><body>Offline synthetic text</body></html>')
        store = DesktopStore(root / 'workspace')
        book = BookRegistry(store).register(source, {
            'title': 'Synthetic', 'author': 'Test', 'translator': 'None', 'language': 'English',
            'edition': 'Test', 'source': 'Synthetic', 'usage_scope': 'Offline test',
        })['book']['book_id']
        api = DesktopAPI(store)
        cleanup.callback(api._tasks.close)
        cleanup.callback(api._book_jobs.close)
        release = threading.Event()
        cleanup.callback(release.set)
        original = api._book_jobs.library.parse

        def paused(*args, **kwargs):
            document = original(*args, **kwargs)
            if not release.wait(30):
                raise TimeoutError('Synthetic browser gate timed out')
            return document

        api._book_jobs.library.parse = paused
        playwright = cleanup.enter_context(sync_playwright())
        browser = playwright.chromium.launch(executable_path=options.chromium_path)
        cleanup.callback(browser.close)
        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        methods = [name for name in dir(api)
                   if not name.startswith('_') and callable(getattr(api, name))]
        page.expose_function('desktopCall', lambda method, args: getattr(api, method)(*args))
        page.add_init_script('window.pywebview={api:Object.fromEntries(' + json.dumps(methods) +
                             '.map(name=>[name,(...args)=>window.desktopCall(name,args)]))};')
        page.goto(Path('tradingagents/desktop/static/index.html').resolve().as_uri())
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="books"]').click()
        record = page.locator(f'[data-book-id="{book}"]')
        record.locator('[data-action="book-parse"]').click()
        record.locator('[data-action="book-cancel"]').wait_for()
        first = api._book_jobs.status(book)['id']
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="books"]').click()
        record.locator('[data-action="book-cancel"]').wait_for()
        assert api._book_jobs.status(book)['id'] == first
        assert record.locator('[data-action="book-parse"]').is_disabled()
        record.locator('[data-action="book-cancel"]').click()
        release.set()
        record.locator('[data-action="book-parse"]:enabled').wait_for()
        assert api._book_jobs.status(book)['status'] == 'cancelled'
        assert api._book_jobs.library.history(book) == []
        record.locator('[data-action="book-resume"]').wait_for()
        page.reload()
        page.wait_for_selector('.page-head')
        page.locator('.sidebar [data-page="books"]').click()
        record.locator('[data-action="book-resume"]').click()
        record.locator('[data-book-parse]').wait_for(state='attached')
        assert api._book_jobs.status(book)['status'] == 'completed'
        assert api._book_jobs.status(book)['resumed_from'] == first
        assert len(api._book_jobs.library.history(book)) == 1
        source_snapshot = store.directory / 'books' / 'originals'
        next(source_snapshot.iterdir()).unlink()
        record.locator('[data-action="book-parse"]:enabled').click()
        record.locator('[role="alert"]').wait_for()
        assert api._book_jobs.status(book)['status'] == 'failed'
        assert len(api._book_jobs.library.history(book)) == 1
        assert not errors, errors
        print('Book extraction cancellation, reload, checkpoint resume and failure acceptance passed')


if __name__ == '__main__':
    main()
