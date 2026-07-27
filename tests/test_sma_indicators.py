import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import gold_service  # noqa: E402


def daily_history(closes, start='2026-06-01'):
    return pd.DataFrame({
        'date': pd.bdate_range(start=start, periods=len(closes)),
        'close': closes,
    })


def daily_ohlc(closes, start='2026-06-01'):
    return pd.DataFrame({
        'date': pd.bdate_range(start=start, periods=len(closes)),
        'high': [100] * len(closes),
        'low': [0] * len(closes),
        'close': closes,
    })


def realtime_quotes(prices):
    return pd.DataFrame({
        '时间': [f'09:{index:02d}:00' for index in range(len(prices))],
        '现价': prices,
    })


class SmaIndicatorApiTests(unittest.TestCase):
    def setUp(self):
        self.client = gold_service.app.test_client()
        gold_service.gold_service._data_cache.clear()

    def request_with_history(self, history, query=''):
        url = '/api/gold/indicators/sma'
        if query:
            url = f'{url}?{query}'
        with patch.object(gold_service.gold_service, 'get_historical_sge_price', return_value=history) as loader:
            response = self.client.get(url)
        return response, loader

    def request_kdj_with_history(self, history, query=''):
        url = '/api/gold/indicators/kdj'
        if query:
            url = f'{url}?{query}'
        with patch.object(gold_service.gold_service, 'get_historical_sge_price', return_value=history) as loader:
            response = self.client.get(url)
        return response, loader

    def request_macd_with_history(self, history, query=''):
        url = '/api/gold/indicators/macd'
        if query:
            url = f'{url}?{query}'
        with patch.object(gold_service.gold_service, 'get_historical_sge_price', return_value=history) as loader:
            response = self.client.get(url)
        return response, loader

    def test_calculates_all_requested_windows_from_complete_history(self):
        response, loader = self.request_with_history(
            daily_history(list(range(1, 41))),
            'days=5&windows=5,10,20,30',
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(loader.call_args.args, (gold_service.GOLD_SYMBOL,))
        self.assertEqual(loader.call_args.kwargs, {'days': None})
        self.assertEqual(body['count'], 5)
        self.assertEqual(body['available_history_count'], 40)
        self.assertEqual(body['data'][0]['date'], '2026-07-20')
        self.assertEqual(body['data'][0]['ma30'], 21.5)
        self.assertEqual(body['latest'], body['data'][0 + 4])
        self.assertEqual(body['latest']['close'], 40.0)
        self.assertEqual(body['latest']['ma5'], 38.0)
        self.assertEqual(body['latest']['ma10'], 35.5)
        self.assertEqual(body['latest']['ma20'], 30.5)
        self.assertEqual(body['latest']['ma30'], 25.5)

    def test_weekend_gap_does_not_count_as_a_window_entry(self):
        history = pd.DataFrame({
            'date': ['2026-07-23', '2026-07-24', '2026-07-27', '2026-07-28', '2026-07-29'],
            'close': [10, 20, 30, 40, 50],
        })
        response, _ = self.request_with_history(history, 'days=1&windows=5')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['latest']['date'], '2026-07-29')
        self.assertEqual(body['latest']['ma5'], 30.0)
        self.assertEqual(body['latest'], body['data'][0])

    def test_insufficient_history_returns_null_for_long_window(self):
        response, _ = self.request_with_history(daily_history(list(range(10))), 'days=10&windows=30')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['available_history_count'], 10)
        self.assertTrue(all(record['ma30'] is None for record in body['data']))

    def test_sorts_dates_and_keeps_last_duplicate_close(self):
        history = pd.DataFrame({
            'date': ['2026-07-27', '2026-07-24', '2026-07-27', 'invalid'],
            'close': [30, '10', 40, 99],
        })
        response, _ = self.request_with_history(history, 'days=2&windows=5')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['available_history_count'], 2)
        self.assertEqual([item['date'] for item in body['data']], ['2026-07-24', '2026-07-27'])
        self.assertEqual(body['latest']['close'], 40.0)

    def test_rejects_invalid_parameters(self):
        for query in ('days=0', 'days=366', 'days=bad', 'windows=7', 'windows=5,7', 'windows=5,5'):
            with self.subTest(query=query):
                response = self.client.get(f'/api/gold/indicators/sma?{query}')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['status'], 'error')

    def test_kdj_uses_full_history_and_standard_9_3_3_smoothing(self):
        response, loader = self.request_kdj_with_history(
            daily_ohlc([50] * 9 + [100]),
            'days=1',
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(loader.call_args.kwargs, {'days': None})
        self.assertEqual(body['basis'], 'high-low-close')
        self.assertEqual(body['parameters']['rsv_window'], 9)
        self.assertEqual(body['latest'], body['data'][0])
        self.assertAlmostEqual(body['latest']['k'], 66.6666666667)
        self.assertAlmostEqual(body['latest']['d'], 55.5555555556)
        self.assertAlmostEqual(body['latest']['j'], 88.8888888889)

    def test_kdj_returns_null_until_nine_trading_days_are_available(self):
        response, _ = self.request_kdj_with_history(daily_ohlc([50] * 8), 'days=8')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['available_history_count'], 8)
        self.assertTrue(all(record['k'] is None for record in body['data']))
        self.assertTrue(all(record['d'] is None for record in body['data']))
        self.assertTrue(all(record['j'] is None for record in body['data']))

    def test_kdj_rejects_invalid_days(self):
        response = self.client.get('/api/gold/indicators/kdj?days=0')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()['status'], 'error')

    def test_macd_uses_full_history_and_standard_12_26_9_ema(self):
        response, loader = self.request_macd_with_history(
            daily_history(list(range(1, 41))),
            'days=1',
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(loader.call_args.kwargs, {'days': None})
        self.assertEqual(body['basis'], 'close')
        self.assertEqual(body['parameters'], {
            'fast_period': 12,
            'slow_period': 26,
            'signal_period': 9,
        })
        self.assertEqual(body['latest'], body['data'][0])
        self.assertAlmostEqual(body['latest']['dif'], 6.3867273176)
        self.assertAlmostEqual(body['latest']['dea'], 6.1145557406)

    def test_macd_starts_at_zero_for_a_single_close_and_validates_days(self):
        response, _ = self.request_macd_with_history(daily_history([100]), 'days=1')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['latest']['dif'], 0.0)
        self.assertEqual(response.get_json()['latest']['dea'], 0.0)

        invalid_response = self.client.get('/api/gold/indicators/macd?days=0')
        self.assertEqual(invalid_response.status_code, 400)
        self.assertEqual(invalid_response.get_json()['status'], 'error')

    def test_history_source_cache_is_reused_within_ttl(self):
        service = gold_service.GoldService()
        service._data_cache.clear()
        history = daily_history([1, 2, 3])
        with patch.object(gold_service.ak, 'spot_hist_sge', return_value=history) as source:
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)

        self.assertEqual(source.call_count, 1)

    def test_historical_cache_ttl_is_shorter_during_weekday_day_session(self):
        trading_time = datetime(2026, 7, 27, 10, 0, tzinfo=gold_service.MARKET_TIMEZONE)
        off_hours = datetime(2026, 7, 27, 18, 0, tzinfo=gold_service.MARKET_TIMEZONE)

        self.assertEqual(
            gold_service.historical_cache_ttl_seconds(trading_time),
            30 * 60,
        )
        self.assertEqual(
            gold_service.historical_cache_ttl_seconds(off_hours),
            12 * 60 * 60,
        )

    def test_indicator_calculation_is_reused_within_history_ttl(self):
        service = gold_service.GoldService()
        history = daily_history([1, 2, 3])
        with patch('gold_service.market_now') as now, patch(
            'gold_service.calculate_sma_indicators',
            return_value=history,
        ) as calculator:
            now.return_value = datetime(2026, 7, 27, 10, 0, tzinfo=gold_service.MARKET_TIMEZONE)
            service.get_cached_indicators(
                gold_service.GOLD_SYMBOL,
                'sma:5',
                lambda: gold_service.calculate_sma_indicators(history, [5]),
            )
            service.get_cached_indicators(
                gold_service.GOLD_SYMBOL,
                'sma:5',
                lambda: gold_service.calculate_sma_indicators(history, [5]),
            )

        self.assertEqual(calculator.call_count, 1)

    def test_history_refresh_invalidates_derived_indicators(self):
        service = gold_service.GoldService()
        indicator_key = f'indicators:{gold_service.GOLD_SYMBOL}:sma:5'
        service._data_cache[indicator_key] = {
            'cached_at': 0,
            'ttl_seconds': 30 * 60,
            'data': daily_history([1]),
        }
        with patch.object(
            gold_service.ak,
            'spot_hist_sge',
            return_value=daily_history([1, 2]),
        ):
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)

        self.assertNotIn(indicator_key, service._data_cache)

    def test_drops_anomalous_terminal_realtime_quote_before_caching(self):
        service = gold_service.GoldService()
        raw_quotes = realtime_quotes([770.0, 771.2, 880.0])
        with patch.object(
            gold_service.ak,
            'spot_quotations_sge',
            return_value=raw_quotes,
        ) as source:
            quotes = service.get_real_time_sge_price(gold_service.GOLD_SYMBOL)
            cached_quotes = service.get_real_time_sge_price(gold_service.GOLD_SYMBOL)

        self.assertEqual(source.call_count, 1)
        self.assertEqual(quotes['现价'].tolist(), [770.0, 771.2])
        self.assertEqual(cached_quotes['现价'].tolist(), [770.0, 771.2])

    def test_keeps_normal_terminal_realtime_quote(self):
        quotes = gold_service.drop_terminal_realtime_outlier(
            realtime_quotes([770.0, 771.2, 771.5]),
            gold_service.GOLD_SYMBOL,
        )

        self.assertEqual(quotes['现价'].tolist(), [770.0, 771.2, 771.5])


if __name__ == '__main__':
    unittest.main()
