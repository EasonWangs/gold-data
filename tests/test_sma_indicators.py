import sys
import unittest
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


class SmaIndicatorApiTests(unittest.TestCase):
    def setUp(self):
        self.client = gold_service.app.test_client()

    def request_with_history(self, history, query=''):
        url = '/api/gold/indicators/sma'
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

    def test_history_source_cache_is_reused_within_ttl(self):
        service = gold_service.GoldService()
        service._data_cache.clear()
        history = daily_history([1, 2, 3])
        with patch.object(gold_service.ak, 'spot_hist_sge', return_value=history) as source:
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)

        self.assertEqual(source.call_count, 1)


if __name__ == '__main__':
    unittest.main()
