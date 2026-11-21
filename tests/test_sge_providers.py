import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gold_service
import sge_client


QUOTES = {
    'heyue': 'Au99.99',
    'times': ['09:00', '09:01', '09:02'],
    'data': ['800', '801', '0'],
    'delaystr': '2026-11-20 09:02:00',
}
HISTORY = {'time': [['2026-11-20', 800, 801, 799, 802]]}


class SgeProviderTests(unittest.TestCase):
    def setUp(self):
        sge_client._load_akshare.cache_clear()
        self.addCleanup(sge_client._load_akshare.cache_clear)
        env = patch.dict('os.environ', {sge_client.PROVIDER_ENV: 'direct'})
        env.start()
        self.addCleanup(env.stop)

    def sdk(self):
        return SimpleNamespace(
            set_timeout=Mock(),
            spot_realtime_sge=Mock(return_value=pd.DataFrame({'现价': [800]})),
            spot_hist_sge=Mock(return_value=pd.DataFrame({'date': ['2026-11-20'], 'close': [800]})),
        )

    def test_default_direct_does_not_import_sdk(self):
        with patch.dict('os.environ', {}, clear=True), patch('sge_client.import_module') as load:
            self.assertEqual(sge_client.configure_provider(), 'direct')
            load.assert_not_called()

    def test_selected_sdk_initializes_native_timeout_once(self):
        sdk = self.sdk()
        with patch.dict('os.environ', {sge_client.PROVIDER_ENV: ' AkShare '}), patch(
            'sge_client.import_module', return_value=sdk,
        ) as load:
            self.assertEqual(sge_client.configure_provider(), 'akshare')
            self.assertEqual(sge_client.configure_provider('akshare'), 'akshare')
        load.assert_called_once_with('akshare')
        sdk.set_timeout.assert_called_once_with((3.05, 10))

    def test_unknown_provider_fails_before_serving_or_fetching(self):
        for value in ('typo', '', 'auto'):
            with self.subTest(value=value), patch.dict('os.environ', {sge_client.PROVIDER_ENV: value}):
                with self.assertRaisesRegex(ValueError, 'direct 或 akshare'):
                    gold_service.GoldService()

    def test_missing_or_old_sdk_fails_clearly_without_unbounded_requests(self):
        with patch('sge_client.import_module', side_effect=ImportError('missing')):
            with self.assertRaisesRegex(RuntimeError, 'requirements.txt'):
                gold_service.GoldService(provider='akshare')
        old_sdk = SimpleNamespace(spot_quotations_sge=Mock(), spot_hist_sge=Mock())
        with patch('sge_client.import_module', return_value=old_sdk):
            with self.assertRaisesRegex(RuntimeError, '1.20.65'):
                gold_service.GoldService(provider='akshare')
        old_sdk.spot_quotations_sge.assert_not_called()

    def test_akshare_uses_renamed_realtime_and_history_entry_points(self):
        sdk = self.sdk()
        with patch('sge_client.import_module', return_value=sdk), patch(
            'sge_client._request_json', side_effect=AssertionError('direct transport must not run'),
        ):
            service = gold_service.GoldService(provider='akshare')
            self.assertIs(service.get_real_time_gold_price(), sdk.spot_realtime_sge.return_value)
            self.assertIs(service.get_historical_gold_price(days=None), sdk.spot_hist_sge.return_value)
        sdk.spot_realtime_sge.assert_called_once_with(symbol='Au99.99')
        sdk.spot_hist_sge.assert_called_once_with(symbol='Au99.99')

    def test_sdk_failure_does_not_silently_fall_back_and_can_recover(self):
        sdk = self.sdk()
        quotes = sdk.spot_realtime_sge.return_value
        sdk.spot_realtime_sge.side_effect = [requests.ReadTimeout('silent source'), quotes]
        with patch('sge_client.import_module', return_value=sdk), patch(
            'sge_client._request_json', side_effect=AssertionError('unexpected fallback'),
        ):
            service = gold_service.GoldService(provider='akshare')
            with self.assertLogs('gold_service', level='ERROR') as logs:
                self.assertIsNone(service.get_real_time_gold_price())
            self.assertIn('provider=akshare', logs.output[0])
            self.assertIn('ReadTimeout', logs.output[0])
            self.assertIs(service.get_real_time_gold_price(), quotes)
            self.assertIs(service.get_real_time_gold_price(), quotes)
        self.assertEqual(sdk.spot_realtime_sge.call_count, 2)

    def test_provider_is_frozen_per_instance_and_new_instance_has_fresh_cache(self):
        sdk = self.sdk()
        with patch('sge_client.import_module', return_value=sdk), patch(
            'sge_client._request_json', return_value=QUOTES,
        ) as direct:
            first = gold_service.GoldService()
            self.assertEqual(first.get_real_time_gold_price()['现价'].tolist(), [800, 801])
            with patch.dict('os.environ', {sge_client.PROVIDER_ENV: 'akshare'}):
                second = gold_service.GoldService()
                self.assertEqual(first.data_provider, 'direct')
                self.assertEqual(first.get_real_time_gold_price()['现价'].tolist(), [800, 801])
                self.assertEqual(second.data_provider, 'akshare')
                self.assertIs(second.get_real_time_gold_price(), sdk.spot_realtime_sge.return_value)
        self.assertEqual(direct.call_count, 1)
        self.assertEqual(sdk.spot_realtime_sge.call_count, 1)

    def test_api_exposes_selected_provider_for_success_and_failure(self):
        sdk = self.sdk()
        with patch('sge_client.import_module', return_value=sdk):
            service = gold_service.GoldService(provider='akshare')
        with patch.object(gold_service, 'gold_service', service):
            client = gold_service.app.test_client()
            info = client.get('/api/info').get_json()
            self.assertEqual(info['data_provider'], 'akshare')
            self.assertEqual(info['supported_data_providers'], ['direct', 'akshare'])
            for path in ('/api/gold/info', '/api/gold/spot_hist_sge', '/api/gold/spot_quotations_sge'):
                with self.subTest(path=path):
                    response = client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.get_json()['data_provider'], 'akshare')
            with patch.object(service, 'get_real_time_sge_price', return_value=None):
                response = client.get('/api/gold/spot_quotations_sge')
                self.assertEqual(response.status_code, 500)
                self.assertEqual(response.get_json()['data_provider'], 'akshare')

    def test_real_sdk_forwards_timeouts_and_matches_direct_parsing(self):
        """Use the installed, pinned SDK; only its network transport is mocked."""
        sdk = sge_client._load_akshare()
        self.addCleanup(sdk.set_timeout, (3.05, 10))
        cases = (
            (sge_client.spot_quotations_sge, QUOTES),
            (sge_client.spot_hist_sge, HISTORY),
        )
        for fetch, payload in cases:
            with self.subTest(fetch=fetch.__name__):
                response = MagicMock()
                response.status_code = 200
                response.json.return_value = payload
                response.__enter__.return_value = response
                with patch('requests.request', return_value=response) as transport:
                    expected = fetch('Au99.99', provider='direct')
                    actual = fetch('Au99.99', provider='akshare')
                pd.testing.assert_frame_equal(actual, expected)
                self.assertEqual(transport.call_count, 2)
                for call in transport.call_args_list:
                    self.assertEqual(call.kwargs['timeout'], (3.05, 10))
                    self.assertEqual(call.kwargs['data'], {'instid': 'Au99.99'})


if __name__ == '__main__':
    unittest.main()
