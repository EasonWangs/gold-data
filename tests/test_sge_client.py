import json
import sys
import threading
import unittest
from datetime import date, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gold_service
import sge_client


def quote_payload():
    return {
        'heyue': 'Au99.99',
        'times': ['09:02', '09:00', '09:01', '09:03'],
        'data': ['802.0', '800.0', '801.0', '0'],
        'delaystr': '2026-11-20 09:03:00',
    }


class SgeClientTests(unittest.TestCase):
    @patch('sge_client.requests.request')
    def test_realtime_preserves_schema_order_and_publication_cutoff(self, request):
        response = request.return_value.__enter__.return_value
        response.json.return_value = quote_payload()

        quotes = sge_client.spot_quotations_sge('Au99.99')

        self.assertEqual(list(quotes.columns), ['品种', '时间', '现价', '更新时间'])
        self.assertEqual(quotes['时间'].tolist(), [time(9), time(9, 1), time(9, 2)])
        self.assertEqual(quotes['现价'].tolist(), [800.0, 801.0, 802.0])
        self.assertEqual(quotes.index.tolist(), [0, 1, 2])
        self.assertEqual(request.call_args.args, ('GET', 'https://www.sge.com.cn/graph/quotations'))
        self.assertEqual(request.call_args.kwargs['data'], {'instid': 'Au99.99'})
        self.assertEqual(request.call_args.kwargs['timeout'], (3.05, 10))
        response.raise_for_status.assert_called_once()
        request.return_value.__exit__.assert_called_once()

    @patch('sge_client.requests.request')
    def test_history_preserves_ohlc_column_mapping_and_numeric_coercion(self, request):
        response = request.return_value.__enter__.return_value
        response.json.return_value = {'time': [
            ['2026-11-19', '800', '802', '799', '804'],
            ['bad-date', '-', '805', '801', '806'],
        ]}

        history = sge_client.spot_hist_sge('Ag99.99')

        self.assertEqual(list(history.columns), ['date', 'open', 'close', 'low', 'high'])
        self.assertEqual(history.iloc[0].tolist(), [date(2026, 11, 19), 800, 802, 799, 804])
        self.assertTrue(pd.isna(history.iloc[1]['date']))
        self.assertTrue(pd.isna(history.iloc[1]['open']))
        self.assertEqual(request.call_args.args, ('POST', 'https://www.sge.com.cn/graph/Dailyhq'))
        self.assertEqual(request.call_args.kwargs['data'], {'instid': 'Ag99.99'})
        self.assertEqual(request.call_args.kwargs['timeout'], (3.05, 10))
        response.raise_for_status.assert_called_once()
        request.return_value.__exit__.assert_called_once()

    def test_http_errors_are_rejected_before_json_parsing(self):
        for fetch in (sge_client.spot_quotations_sge, sge_client.spot_hist_sge):
            with self.subTest(fetch=fetch.__name__), patch('sge_client.requests.request') as request:
                response = request.return_value.__enter__.return_value
                response.raise_for_status.side_effect = requests.HTTPError('503')
                with self.assertRaises(requests.HTTPError):
                    fetch('Au99.99')
                response.json.assert_not_called()
                request.return_value.__exit__.assert_called_once()

    def test_connect_and_read_timeouts_propagate_without_automatic_retries(self):
        for fetch in (sge_client.spot_quotations_sge, sge_client.spot_hist_sge):
            for error_type in (requests.ConnectTimeout, requests.ReadTimeout):
                with self.subTest(fetch=fetch.__name__, error=error_type.__name__):
                    with patch('sge_client.requests.request', side_effect=error_type('timed out')) as request:
                        with self.assertRaises(error_type):
                            fetch('Au99.99')
                        self.assertEqual(request.call_count, 1)
                        self.assertEqual(request.call_args.kwargs['timeout'], (3.05, 10))

    def test_bad_json_does_not_poison_cache_and_next_request_recovers(self):
        service = gold_service.GoldService()
        with patch('sge_client.requests.request') as request:
            response = request.return_value.__enter__.return_value
            response.json.side_effect = [ValueError('invalid JSON'), quote_payload()]
            with self.assertLogs('gold_service', level='ERROR'):
                self.assertIsNone(service.get_real_time_gold_price())
            self.assertEqual(service.get_real_time_gold_price()['现价'].tolist(), [800, 801, 802])
            self.assertEqual(request.call_count, 2)

    def test_empty_source_payloads_keep_the_expected_columns(self):
        cases = (
            (sge_client.spot_quotations_sge,
             {'heyue': 'Au99.99', 'times': [], 'data': [], 'delaystr': ''},
             ['品种', '时间', '现价', '更新时间']),
            (sge_client.spot_hist_sge, {'time': []}, ['date', 'open', 'close', 'low', 'high']),
        )
        for fetch, payload, columns in cases:
            with self.subTest(fetch=fetch.__name__), patch('sge_client.requests.request') as request:
                request.return_value.__enter__.return_value.json.return_value = payload
                data = fetch('Au99.99')
                self.assertTrue(data.empty)
                self.assertEqual(list(data.columns), columns)

    def test_real_http_read_timeout_returns_api_error_then_recovers(self):
        """Exercise Requests' actual socket timeout against a local silent peer."""
        slow = threading.Event()
        slow.set()
        release = threading.Event()
        started = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if slow.is_set():
                    started.set()
                    release.wait(3)
                    return
                body = json.dumps(quote_payload()).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            url = f'http://127.0.0.1:{server.server_port}'
            with patch.object(sge_client, 'SGE_BASE_URL', url), patch.object(
                sge_client, 'SGE_REQUEST_TIMEOUT', (0.5, 0.1)
            ), patch.object(gold_service, 'gold_service', gold_service.GoldService()), patch.dict(
                'os.environ', {'NO_PROXY': '127.0.0.1', 'no_proxy': '127.0.0.1'}
            ):
                client = gold_service.app.test_client()
                with self.assertLogs('gold_service', level='ERROR'):
                    failed = client.get('/api/gold/spot_quotations_sge')
                self.assertTrue(started.is_set())
                self.assertEqual(failed.status_code, 500)
                self.assertEqual(failed.get_json()['status'], 'error')
                self.assertEqual(client.get('/api/info').status_code, 200)
                slow.clear()
                release.set()
                recovered = client.get('/api/gold/spot_quotations_sge')
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual([row['现价'] for row in recovered.get_json()['data']], [800, 801, 802])
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
