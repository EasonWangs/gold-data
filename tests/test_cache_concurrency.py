import sys
import threading
import unittest
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gold_service


def history(close):
    return pd.DataFrame({'date': ['2026-11-20'], 'close': [close]})


class CacheConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.service = gold_service.GoldService()
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.addCleanup(self.pool.shutdown, wait=True)
        self.release = threading.Event()
        # Release blocked loaders before joining the pool, even on assertion failure.
        self.addCleanup(self.release.set)
        self.started = threading.Event()
        self.waiting = threading.Event()
        waiting = self.waiting

        class ObservedFuture(Future):
            def result(self, timeout=None):
                # A deterministic handshake: the follower has joined the active
                # refresh. Avoid sleep-based timing assertions in these tests.
                waiting.set()
                return super().result(timeout)

        patcher = patch('gold_service.Future', ObservedFuture)
        patcher.start()
        self.addCleanup(patcher.stop)

    def block(self):
        self.started.set()
        if not self.release.wait(3):
            raise TimeoutError('test did not release the upstream loader')

    def load_history(self, data, force_refresh=False):
        with patch.object(gold_service.sge_client, 'spot_hist_sge', return_value=data):
            return self.service.get_historical_sge_price(
                gold_service.GOLD_SYMBOL, days=None, force_refresh=force_refresh,
            )

    def test_slow_gold_realtime_does_not_block_silver_history_or_cached_reads(self):
        gold = pd.DataFrame({'现价': [800]})
        silver = pd.DataFrame({'现价': [7000]})
        daily = history(800)

        def quotes(symbol, *, provider):
            if symbol == gold_service.GOLD_SYMBOL:
                self.block()
                return gold
            return silver

        with patch.object(gold_service.sge_client, 'spot_quotations_sge', side_effect=quotes), patch.object(
            gold_service.sge_client, 'spot_hist_sge', return_value=daily,
        ) as daily_fetch:
            slow = self.pool.submit(self.service.get_real_time_gold_price)
            self.assertTrue(self.started.wait(1))
            other_metal = self.pool.submit(self.service.get_real_time_sge_price, gold_service.SILVER_SYMBOL)
            self.assertIs(other_metal.result(timeout=1), silver)
            cold = self.pool.submit(self.service.get_historical_sge_price, gold_service.GOLD_SYMBOL, None)
            self.assertIs(cold.result(timeout=1), daily)
            warm = self.pool.submit(self.service.get_historical_sge_price, gold_service.GOLD_SYMBOL, None)
            self.assertIs(warm.result(timeout=1), daily)
            self.assertEqual(daily_fetch.call_count, 1)
            self.assertFalse(slow.done())
            self.release.set()
            self.assertIs(slow.result(timeout=1), gold)

    def test_same_key_concurrent_misses_share_one_refresh(self):
        data = history(800)

        def fetch():
            self.block()
            return data

        loader = Mock(side_effect=fetch)
        first = self.pool.submit(self.service._get_cached_data, 'history', loader, 60)
        self.assertTrue(self.started.wait(1))
        second = self.pool.submit(self.service._get_cached_data, 'history', loader, 60)
        self.assertTrue(self.waiting.wait(1))
        self.release.set()
        self.assertIs(first.result(timeout=1), data)
        self.assertIs(second.result(timeout=1), data)
        self.assertIs(self.service._get_cached_data('history', loader, 60), data)
        self.assertEqual(loader.call_count, 1)

    def test_forced_refreshes_coalesce_while_normal_read_uses_fresh_cache(self):
        old, new = history(800), history(810)
        self.service._get_cached_data('history', lambda: old, 60)

        def fetch():
            self.block()
            return new

        loader = Mock(side_effect=fetch)
        first = self.pool.submit(self.service._get_cached_data, 'history', loader, 60, force_refresh=True)
        self.assertTrue(self.started.wait(1))
        normal = self.pool.submit(self.service._get_cached_data, 'history', loader, 60)
        self.assertIs(normal.result(timeout=1), old)
        second = self.pool.submit(self.service._get_cached_data, 'history', loader, 60, force_refresh=True)
        self.assertTrue(self.waiting.wait(1))
        self.release.set()
        self.assertIs(first.result(timeout=1), new)
        self.assertIs(second.result(timeout=1), new)
        self.assertEqual(loader.call_count, 1)

    def test_failure_is_shared_with_waiters_and_next_request_can_retry(self):
        def fail():
            self.block()
            raise requests.ReadTimeout('upstream silent')

        loader = Mock(side_effect=fail)
        first = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.assertTrue(self.started.wait(1))
        second = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.assertTrue(self.waiting.wait(1))
        self.release.set()
        for future in (first, second):
            with self.assertRaises(requests.ReadTimeout):
                future.result(timeout=1)
        self.assertEqual(loader.call_count, 1)
        recovered = history(810)
        self.assertIs(self.service._get_cached_data('quotes', lambda: recovered, 60), recovered)

    def test_none_result_is_shared_but_not_cached(self):
        def empty():
            self.block()
            return None

        loader = Mock(side_effect=empty)
        first = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.assertTrue(self.started.wait(1))
        second = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.assertTrue(self.waiting.wait(1))
        self.release.set()
        self.assertIsNone(first.result(timeout=1))
        self.assertIsNone(second.result(timeout=1))
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(self.service._get_cached_data('quotes', lambda: 'recovered', 60), 'recovered')

    def test_waiter_timeout_does_not_cancel_refresh_or_create_duplicate_work(self):
        def fetch():
            self.block()
            return 'recovered'

        loader = Mock(side_effect=fetch)
        first = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.assertTrue(self.started.wait(1))
        with patch.object(gold_service, 'CACHE_REFRESH_WAIT_SECONDS', 0.05):
            with self.assertRaises(TimeoutError):
                self.service._get_cached_data('quotes', loader, 60)
        self.assertFalse(first.done())
        second = self.pool.submit(self.service._get_cached_data, 'quotes', loader, 60)
        self.release.set()
        self.assertEqual(first.result(timeout=1), 'recovered')
        self.assertEqual(second.result(timeout=1), 'recovered')
        self.assertEqual(loader.call_count, 1)

    def test_cache_expires_and_ttl_change_invalidates_it(self):
        loader = Mock(side_effect=['first', 'second', 'third'])
        with patch('gold_service.time.monotonic', return_value=100) as clock:
            self.assertEqual(self.service._get_cached_data('quotes', loader, 60), 'first')
            clock.return_value = 159
            self.assertEqual(self.service._get_cached_data('quotes', loader, 60), 'first')
            clock.return_value = 160
            self.assertEqual(self.service._get_cached_data('quotes', loader, 60), 'second')
            self.assertEqual(self.service._get_cached_data('quotes', loader, 30), 'third')
        self.assertEqual(loader.call_count, 3)

    def test_timeout_at_each_quote_entry_point_is_recoverable(self):
        cases = (
            ('spot_quotations_sge', self.service.get_real_time_gold_price),
            ('spot_hist_sge', lambda: self.service.get_historical_gold_price(days=None)),
        )
        for source, fetch in cases:
            with self.subTest(source=source):
                data = pd.DataFrame({'现价': [800]}) if source == 'spot_quotations_sge' else history(800)
                with patch.object(gold_service.sge_client, source, side_effect=[requests.ConnectTimeout(), data]) as loader:
                    with self.assertLogs('gold_service', level='ERROR'):
                        self.assertIsNone(fetch())
                    self.assertIs(fetch(), data)
                    self.assertIs(fetch(), data)
                    self.assertEqual(loader.call_count, 2)

    def test_failed_forced_history_refresh_preserves_good_cache_and_indicators(self):
        old = self.load_history(history(800))
        calculate = Mock(return_value='valid indicator')
        self.service.get_cached_indicators('Au99.99', 'sma:5', calculate, history_data=old)
        with patch.object(gold_service.sge_client, 'spot_hist_sge', side_effect=requests.ReadTimeout()):
            with self.assertLogs('gold_service', level='ERROR'):
                self.assertIsNone(self.service.get_historical_gold_price(force_refresh=True))
        self.assertIs(self.service.get_historical_gold_price(days=None), old)
        result = self.service.get_cached_indicators('Au99.99', 'sma:5', calculate, history_data=old)
        self.assertEqual(result, 'valid indicator')
        self.assertEqual(calculate.call_count, 1)

    def test_history_refresh_during_calculation_does_not_restore_stale_indicators(self):
        old = self.load_history(history(800))

        def calculate_old():
            self.block()
            return 'old indicator'

        old_job = self.pool.submit(
            self.service.get_cached_indicators, 'Au99.99', 'sma:5', calculate_old, history_data=old,
        )
        self.assertTrue(self.started.wait(1))
        refresh = self.pool.submit(self.load_history, history(810), True)
        new = refresh.result(timeout=1)
        calculate_new = Mock(return_value='new indicator')
        new_job = self.pool.submit(
            self.service.get_cached_indicators, 'Au99.99', 'sma:5', calculate_new, history_data=new,
        )
        self.assertEqual(new_job.result(timeout=1), 'new indicator')
        self.assertFalse(old_job.done())
        self.release.set()
        self.assertEqual(old_job.result(timeout=1), 'old indicator')
        self.assertEqual(self.service.get_cached_indicators(
            'Au99.99', 'sma:5', calculate_new, history_data=new,
        ), 'new indicator')
        self.assertEqual(calculate_new.call_count, 1)

    def test_history_refresh_before_old_calculation_starts_does_not_cache_it(self):
        old = self.load_history(history(800))
        new = self.load_history(history(810), force_refresh=True)
        old_calc = Mock(return_value='old indicator')
        self.assertEqual(self.service.get_cached_indicators(
            'Au99.99', 'sma:5', old_calc, history_data=old,
        ), 'old indicator')
        new_calc = Mock(return_value='new indicator')
        for _ in range(2):
            self.assertEqual(self.service.get_cached_indicators(
                'Au99.99', 'sma:5', new_calc, history_data=new,
            ), 'new indicator')
        self.assertEqual(new_calc.call_count, 1)

    def test_failed_expired_history_does_not_serve_old_indicators_as_success(self):
        with patch.object(gold_service, 'gold_service', self.service), patch.object(
            gold_service.sge_client, 'spot_hist_sge', side_effect=[history(800), requests.ReadTimeout()],
        ), patch('gold_service.time.monotonic', return_value=100) as clock:
            client = gold_service.app.test_client()
            self.assertEqual(client.get('/api/gold/indicators/sma').status_code, 200)
            clock.return_value = 100 + gold_service.HISTORICAL_OFF_HOURS_CACHE_TTL_SECONDS
            with self.assertLogs('gold_service', level='ERROR'):
                response = client.get('/api/gold/indicators/sma')
            self.assertEqual(response.status_code, 500)


if __name__ == '__main__':
    unittest.main()
