import queue
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from collection_pipeline import TaskStore, collect_web
from youtube_ledger import Ledger
from ytscrape_metrics import interaction_metrics
from test_core import app


BRANDS = [{"name": name, "words": [name], "official": "", "query": name} for name in ("Alpha", "Beta")]


def row(video_id, **kwargs):
    return dict(video_id=video_id, title="Alpha Beta review", description="", brand="Alpha",
                published_at="2026-01-01", channel_id="UCcreator", **kwargs)


class Collector:
    def __init__(self):
        self.queries, self.reads = [], []
        self.search_error = None
        self.detail_error = None
        self.stopped = False
        self.rows = {}

    def check_stop(self):
        if self.stopped:
            raise InterruptedError()

    def resolve_official(self, account):
        return account or None

    def search(self, query, limit):
        self.queries.append(query)
        if self.search_error and query.startswith("Alpha"):
            raise self.search_error
        yield query.split()[0] + "1"
        yield query.split()[0] + "2"

    def video(self, brand, video_id):
        self.reads.append(video_id)
        if self.detail_error:
            raise self.detail_error
        return self.rows.get(video_id, row(video_id))

    def pause(self):
        self.check_stop()


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'ledger.sqlite3'
        self.ledger = Ledger(self.path)
        self.run = self.ledger.create_run({"depth": 30, "limit": 1}, "采集")
        self.collector = Collector()

    def collect(self, limit=100, brands=None):
        return collect_web(self.ledger, self.collector, brands or BRANDS, [2026], 30, limit,
                           self.run, self.ledger.add_video, lambda _: None)

    def test_budget_resumes_without_search_and_rotates_brands(self):
        state, stats = self.collect(1)
        self.assertEqual(state, 'budget')
        self.assertEqual(stats['unique'], 4)
        self.assertEqual(self.collector.reads, ['Alpha1'])
        self.ledger = Ledger(self.path)  # A fresh process can read the durable queue.
        self.collector = Collector()
        self.collect(1)
        self.assertEqual(self.collector.queries, [])
        self.assertEqual(self.collector.reads, ['Beta1'])
        self.assertEqual(self.collect()[0], 'complete')
        self.assertEqual(len(self.ledger.rows(self.run)), 4)

    def test_one_brand_failure_does_not_abort_the_other(self):
        self.collector.search_error = RuntimeError('temporary search failure')
        state, stats = self.collect()
        self.assertEqual(state, 'partial')
        self.assertEqual(stats['jobs']['error'], 2)
        self.assertEqual({r['brand'] for r in self.ledger.rows()}, {'Beta'})
        self.collector.search_error = None
        self.assertEqual(self.collect()[0], 'complete')
        self.assertEqual(len(self.ledger.rows()), 4)

    def test_missing_date_remains_retryable(self):
        self.collector.rows['Alpha1'] = dict(row('Alpha1'), published_at='')
        self.assertEqual(self.collect()[0], 'partial')
        self.assertEqual(TaskStore(self.ledger).counts(self.run)['items']['missing_date'], 1)
        self.collector.rows.clear()
        self.assertEqual(self.collect()[0], 'complete')
        self.assertEqual(len(self.ledger.rows()), 4)

    def test_detail_failure_is_retried_on_resume(self):
        self.collector.detail_error = RuntimeError('temporary failure')
        self.assertEqual(self.collect()[0], 'partial')
        self.collector.detail_error = None
        self.assertEqual(self.collect()[0], 'complete')

    def test_deleted_video_is_never_reintroduced(self):
        self.ledger.add_video(row('Alpha1'))
        self.ledger.set_deleted(['Alpha1'])
        self.collect()
        self.assertNotIn('Alpha1', self.collector.reads)
        self.assertEqual(TaskStore(self.ledger).counts(self.run)['items']['deleted'], 1)

    def test_official_rejection_does_not_starve_next_brand(self):
        brands = [dict(BRANDS[0], official='UCcreator'), BRANDS[1]]
        self.collect(1, brands)
        self.collect(1, brands)
        self.assertEqual(self.collector.reads, ['Alpha1', 'Beta1'])
        self.assertEqual(len(self.ledger.rows()), 1)

    def test_missing_channel_and_empty_official_are_not_equal(self):
        self.collector.rows['Alpha1'] = dict(row('Alpha1'), channel_id=None)
        self.collect()
        self.assertEqual(len(self.ledger.rows()), 4)

    def test_search_accepts_brand_in_description_without_brand_in_title(self):
        self.collector.rows['Alpha1'] = dict(row('Alpha1'), title='An unrelated title', description='Testing Alpha today')
        self.collect()
        self.assertIn('Alpha1', {r['video_id'] for r in self.ledger.rows()})

    def test_search_rejects_without_brand_in_title_or_description(self):
        self.collector.rows['Alpha1'] = dict(row('Alpha1'), title='An unrelated title', description='No match')
        self.collect()
        self.assertNotIn('Alpha1', {r['video_id'] for r in self.ledger.rows()})
        self.assertEqual(TaskStore(self.ledger).counts(self.run)['items']['unrelated'], 1)

    def test_interruption_preserves_discovered_ids(self):
        def search(query, limit):
            yield 'saved1'
            raise InterruptedError()
        self.collector.search = search
        self.assertEqual(self.collect()[0], 'stopped')
        self.assertEqual(TaskStore(self.ledger).counts(self.run)['unique'], 1)
        self.assertEqual(self.collector.reads, [])

    def test_blocked_state_is_not_success(self):
        self.collector.detail_error = RuntimeError('HTTP 429')
        state, stats = self.collect()
        self.assertEqual(state, 'blocked')
        self.assertEqual(stats['items']['pending'], 3)

    def test_resume_ignores_budget_but_respects_search_configuration(self):
        self.collect(1)
        store = TaskStore(self.ledger)
        self.assertEqual(store.resume({'depth': 30, 'limit': 500}, '采集'), self.run)
        self.assertIsNone(store.resume({'depth': 300, 'limit': 500}, '采集'))

    def test_shared_video_is_fetched_once_for_multiple_brands(self):
        self.collector.search = lambda query, limit: iter(['shared'])
        self.collect()
        self.assertEqual(self.collector.reads, ['shared'])
        self.assertEqual(set(self.ledger.rows()[0]['brands']), {'Alpha', 'Beta'})

    def test_failed_official_resolution_only_defers_that_brand(self):
        def official(account):
            if account:
                raise RuntimeError('channel temporarily unavailable')
            return None
        self.collector.resolve_official = official
        state, stats = self.collect(brands=[dict(BRANDS[0], official='@Example'), BRANDS[1]])
        self.assertEqual(state, 'partial')
        self.assertEqual(stats['unresolved_official'], ['Alpha'])
        self.assertEqual({r['brand'] for r in self.ledger.rows()}, {'Beta'})
        self.assertEqual(stats['items']['pending'], 2)

    def test_year_exclusion_is_visible_and_does_not_repeat_on_resume(self):
        self.collector.rows['Alpha1'] = dict(row('Alpha1'), published_at='2020-01-01')
        self.collect()
        self.assertEqual(TaskStore(self.ledger).counts(self.run)['items']['year'], 1)
        self.collector.reads.clear()
        self.collect()
        self.assertEqual(self.collector.reads, [])


class ApiPipelineTests(unittest.TestCase):
    def test_api_budget_continues_next_brand_and_page_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = app.App.__new__(app.App)
            instance.ledger = Ledger(Path(folder) / 'api.sqlite3')
            instance.stop = threading.Event()
            instance.events = queue.Queue()
            run = instance.ledger.create_run({})
            seen = []

            class API:
                def __init__(self, key, stop):
                    self.search_calls = 0

                def resolve_official(self, account):
                    return None

                def search(self, query, start, end, token):
                    self.search_calls += 1
                    seen.append((query, token))
                    return {'items': [], 'nextPageToken': 'page-two' if not token else ''}

            with patch.object(app, 'YouTubeApi', API), patch.object(app, 'candidates_from_page', return_value=[]):
                instance.run_api('synthetic', BRANDS, [2024], 2, 1, run)
                self.assertEqual(TaskStore(instance.ledger).result(run)[0], 'budget')
                instance.run_api('synthetic', BRANDS, [2024], 2, 1, run)
                self.assertEqual(seen[:2], [('Alpha', ''), ('Beta', '')])
                instance.run_api('synthetic', BRANDS, [2024], 2, 100, run)
                self.assertEqual(TaskStore(instance.ledger).result(run)[0], 'complete')
                self.assertEqual(len(seen), 16)
                self.assertEqual(sum(token == 'page-two' for _, token in seen), 8)
                instance.run_api('synthetic', BRANDS, [2024], 2, 100, run)
                self.assertEqual(len(seen), 16)  # Empty checkpoint means finished, not restart.

    def test_api_error_preserves_page_and_does_not_abort_other_brand(self):
        with tempfile.TemporaryDirectory() as folder:
            instance = app.App.__new__(app.App)
            instance.ledger = Ledger(Path(folder) / 'api.sqlite3')
            instance.stop = threading.Event()
            instance.events = queue.Queue()
            run = instance.ledger.create_run({})
            seen = []

            class API:
                def __init__(self, key, stop):
                    self.search_calls = 0

                def resolve_official(self, account):
                    return None

                def search(self, query, start, end, token):
                    self.search_calls += 1
                    seen.append(query)
                    if query == 'Alpha':
                        raise RuntimeError('temporary failure')
                    return {'items': []}

            with patch.object(app, 'YouTubeApi', API), patch.object(app, 'candidates_from_page', return_value=[]):
                instance.run_api('synthetic', BRANDS, [2024], 2, 100, run)
            state, stats = TaskStore(instance.ledger).result(run)
            self.assertEqual(state, 'partial')
            self.assertEqual(stats['remaining_queries'], 4)
            self.assertIn('Beta', seen)


class SearchRoutingTests(unittest.TestCase):
    def test_interaction_data_handles_exact_likes_and_approximate_subscribers(self):
        response = {'videoPrimaryInfoRenderer': {'segmentedLikeDislikeButtonViewModel': {
            'button': {'iconName': 'LIKE', 'title': '1.2K', 'accessibilityText': 'like along with 1,234 other people'}}},
            'videoOwnerRenderer': {'subscriberCountText': {'simpleText': '12.3K subscribers'}},
            'engagementPanelTitleHeaderRenderer': {'title': {'simpleText': 'Comments'}, 'contextualInfo': {'simpleText': '45'}}}
        self.assertEqual(interaction_metrics(response), {'likes': 1234, 'comments': 45, 'subscribers': '约 12.3K subscribers'})

    def test_metadata_backend_provides_date_and_full_description_without_watch_page(self):
        collector = app.WebCollector(threading.Event(), lambda _: None)
        collector.metadata_client = SimpleNamespace(video=lambda _: SimpleNamespace(
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc), uploaded_at=None,
            title='Example review', description='Example full description', channel_id='UCexample',
            channel='Example creator', views=23))
        with patch.object(collector, 'extract', side_effect=AssertionError('watch page should not be used')):
            row = collector.video({'name': 'Example', 'words': ['Example']}, 'sample-id')
        self.assertEqual(row['published_at'][:10], '2025-06-01')
        self.assertEqual(row['description'], 'Example full description')
        self.assertEqual(row['views'], 23)
        self.assertIsNone(row['likes'])

    def test_access_block_warning_stops_incomplete_metadata_processing(self):
        collector = app.WebCollector(threading.Event(), lambda _: None)
        for warning in ('HTTP Error 429: Too Many Requests', "Sign in to confirm you're not a bot"):
            with self.assertRaisesRegex(RuntimeError, '访问受限'):
                collector.warning(warning)

    def test_both_sort_orders_use_supported_extractors_and_deduplicate(self):
        from yt_dlp.extractor.youtube import YoutubeSearchIE, YoutubeSearchURLIE
        collector = app.WebCollector(threading.Event(), lambda _: None)
        urls = []

        class YDL:
            def __init__(self, options):
                self.options = options

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                urls.append(url)
                return {'entries': [{'id': 'shared'}, {'id': str(len(urls))}]}

        with patch.object(collector.yt_dlp, 'YoutubeDL', YDL):
            self.assertEqual(list(collector.search('Example & review', 30)), ['shared', '1', '2'])
        self.assertTrue(YoutubeSearchIE.suitable(urls[0]))
        self.assertTrue(YoutubeSearchURLIE.suitable(urls[1]))
        self.assertIn('search_query=Example+%26+review', urls[1])

    def test_failed_first_sort_still_yields_second_sort_and_reports_partial(self):
        collector = app.WebCollector(threading.Event(), lambda _: None)
        seen = []

        class YDL:
            def __init__(self, options):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def extract_info(self, url, download):
                if url.startswith('ytsearch'):
                    raise RuntimeError('temporary error')
                return {'entries': [{'id': 'newest'}]}

        with patch.object(collector.yt_dlp, 'YoutubeDL', YDL):
            with self.assertRaisesRegex(RuntimeError, '部分排序'):
                for video in collector.search('Example', 30):
                    seen.append(video)
        self.assertEqual(seen, ['newest'])


if __name__ == '__main__':
    unittest.main()
