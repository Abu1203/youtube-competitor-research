import csv
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook
from youtube_ledger import Ledger
from collection_pipeline import TaskStore, collect_web
from one_click_export import export_bundle
from test_core import app
import scrapetube
import ytscrape
from types import SimpleNamespace

BRAND = {'name': 'Example', 'words': ['Example'], 'official': '', 'query': 'Example'}


class Collector:
    def __init__(self, *args):
        self.queries = []
        self.channel_queries = []

    def check_stop(self):
        pass

    def resolve_official(self, account):
        return None

    def search_deep(self, query, limit):
        self.queries.append(query)
        yield from ('one', 'two')

    def channel_videos_deep(self, channel, limit, brand=None):
        self.channel_queries.append(channel)
        yield 'three'

    def video(self, brand, video_id):
        return {'video_id': video_id, 'brand': brand['name'], 'title': 'Example review',
                'description': 'Example description', 'published_at': '2026-03-01',
                'channel_id': 'UCexample', 'channel_name': 'Creator', 'views': 100,
                'video_url': 'https://example.com/' + video_id,
                'channel_url': 'https://example.com/channel', 'likes': None, 'comments': None}

    def enrich_row(self, row):
        return dict(row, likes=5, comments=2, subscribers=20, region='US')

    def pause(self):
        pass


class OneClickTests(unittest.TestCase):
    def test_transient_search_error_retries_without_duplicate_video_ids(self):
        collector = app.WebCollector(threading.Event(), lambda _: None)
        calls = []
        def get_search(query, limit, sort_by, sleep):
            calls.append(sort_by)
            yield {'videoId': 'one'}
            if sort_by == 'relevance' and calls.count('relevance') == 1:
                raise ValueError('temporary malformed response')
            yield {'videoId': 'two'}
        with patch.object(scrapetube, 'get_search', side_effect=get_search):
            with patch.object(collector.stop, 'wait', return_value=False):
                self.assertEqual(list(collector.search_deep('Example & test', 3)), ['one', 'two'])
        self.assertEqual(calls, ['relevance', 'relevance', 'upload_date'])

    def test_channel_scan_avoids_unrelated_titles_before_detail_requests(self):
        collector = app.WebCollector(threading.Event(), lambda _: None)
        client = SimpleNamespace(channel_videos=lambda *args, **kwargs: [
            SimpleNamespace(video_id='relevant', title='Example review'),
            SimpleNamespace(video_id='unrelated', title='Something else'),
            SimpleNamespace(video_id='unknown', title='')])
        with patch.object(ytscrape, 'YouTube', return_value=client), \
             patch.object(collector, 'channel_videos', return_value=iter(['relevant', 'extra'])):
            self.assertEqual(list(collector.channel_videos_deep('UCtest', 10, BRAND)), ['relevant', 'unknown', 'extra'])

    def test_single_start_continues_budgets_scans_channel_and_exports_all_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = app.App.__new__(app.App)
            instance.ledger = Ledger(root / 'test.sqlite3')
            instance.stop = threading.Event()
            instance.events = queue.Queue()
            run = instance.ledger.create_run({})
            collector = Collector()
            with patch.object(app, 'APP_DIR', root), patch.object(app, 'TEMPLATE_PATH', root / 'missing.xlsx'), \
                 patch.object(app, 'WebCollector', return_value=collector):
                instance.run_web([BRAND], [2026], 5, 1, run, False, False, True, True)
            state, summary = TaskStore(instance.ledger).result(run)
            self.assertEqual(state, 'complete')
            self.assertEqual(len(instance.ledger.rows(run)), 3)
            self.assertEqual(collector.channel_queries, ['UCexample'])
            self.assertTrue(any('after:2025-12-31 before:2026-07-01' in q for q in collector.queries))
            self.assertTrue(any('after:2026-06-30 before:2027-01-01' in q for q in collector.queries))
            directory = Path(summary['export_directory'])
            self.assertEqual({p.name for p in directory.iterdir()}, {'视频明细.xlsx', '视频描述.csv', '品牌年度汇总.xlsx'})
            book = load_workbook(directory / '视频明细.xlsx')
            self.addCleanup(book.close)
            self.assertEqual(book.active.max_row, 4)
            self.assertEqual(book.active['A1'].value, '品牌')
            self.assertEqual(book.active['E2'].value, 100)
            self.assertEqual(book.active['F2'].value, 5)
            with (directory / '视频描述.csv').open(encoding='utf-8-sig', newline='') as f:
                descriptions = list(csv.DictReader(f))
            self.assertEqual(len(descriptions), 3)
            self.assertTrue(all(r['完整描述'] == 'Example description' for r in descriptions))
            summary_book = load_workbook(directory / '品牌年度汇总.xlsx', data_only=True)
            self.addCleanup(summary_book.close)
            self.assertEqual([summary_book.active.cell(2, i).value for i in (1, 3, 4)], ['Example', 1, 3])
            self.assertAlmostEqual(summary_book.active['E2'].value, .03)

    def test_title_rule_rejects_description_only_match(self):
        with tempfile.TemporaryDirectory() as folder:
            ledger = Ledger(Path(folder) / 'test.sqlite3')
            run = ledger.create_run({})
            collector = Collector()
            collector.video = lambda brand, vid: {'video_id': vid, 'brand': brand['name'],
                'title': 'Other title', 'description': 'Example description', 'published_at': '2026-01-01'}
            state, stats = collect_web(ledger, collector, [BRAND], [2026], 5, 10, run,
                                       ledger.add_video, lambda _: None, deep=True, strict_title=True)
            self.assertEqual(state, 'complete')
            self.assertEqual(stats['items']['unrelated'], 2)
            self.assertEqual(ledger.rows(), [])

    def test_export_does_not_reintroduce_old_description_only_brand_association(self):
        with tempfile.TemporaryDirectory() as folder:
            row = {'video_id': 'one', 'title': 'Example review', 'brand': 'Example',
                   'brands': ['Example', 'Other'], 'published_at': '2026-01-01',
                   'description': 'Other brand in description', 'video_url': 'https://example.com/one'}
            brands = [BRAND, {'name': 'Other'}]
            paths = export_bundle([row], Path(folder), Path(folder) / 'none.xlsx', brands, [2026])
            book = load_workbook(paths[0])
            self.addCleanup(book.close)
            self.assertEqual(book.active.max_row, 2)
            self.assertEqual(book.active['A2'].value, 'Example')

    def test_rate_limit_exports_partial_and_remains_resumable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = app.App.__new__(app.App)
            instance.ledger = Ledger(root / 'test.sqlite3')
            instance.stop = threading.Event()
            instance.events = queue.Queue()
            run = instance.ledger.create_run({})
            collector = Collector()
            original = collector.video
            def video(brand, vid):
                if vid == 'two':
                    raise RuntimeError('HTTP 429')
                return original(brand, vid)
            collector.video = video
            with patch.object(app, 'APP_DIR', root), patch.object(app, 'TEMPLATE_PATH', root / 'missing.xlsx'), \
                 patch.object(app, 'WebCollector', return_value=collector):
                instance.run_web([BRAND], [2026], 5, 10, run, False, False, True, True)
            state, summary = TaskStore(instance.ledger).result(run)
            self.assertEqual(state, 'blocked')
            self.assertEqual(len(instance.ledger.rows(run)), 1)
            self.assertTrue((Path(summary['export_directory']) / '视频明细.xlsx').exists())

    def test_permanent_detail_failures_do_not_loop_across_automatic_batches(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            instance = app.App.__new__(app.App)
            instance.ledger = Ledger(root / 'test.sqlite3')
            instance.stop = threading.Event()
            instance.events = queue.Queue()
            run = instance.ledger.create_run({})
            collector = Collector()
            calls = []
            def fail(brand, vid):
                calls.append(vid)
                raise ValueError('permanently unavailable')
            collector.video = fail
            with patch.object(app, 'APP_DIR', root), patch.object(app, 'TEMPLATE_PATH', root / 'missing.xlsx'), \
                 patch.object(app, 'WebCollector', return_value=collector):
                instance.run_web([BRAND], [2026], 5, 1, run, False, False, True, True)
            state, summary = TaskStore(instance.ledger).result(run)
            self.assertEqual(state, 'partial')
            self.assertEqual(sorted(calls), ['one', 'two'])
            self.assertTrue((Path(summary['export_directory']) / '采集结果说明.txt').exists())

