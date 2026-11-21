import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gold_service
import market_config
import sge_client


class MarketConfigTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / 'market_data.json'
        self.manager = market_config.MarketServiceManager(gold_service.GoldService, fallback='direct', path=self.path)
        patches = (
            patch.object(gold_service, 'market_service_manager', self.manager),
            patch.object(sge_client, '_load_akshare', return_value=SimpleNamespace()),
            patch.dict(os.environ, {'GOLD_ADMIN_TOKEN': 'test-only-admin'}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = gold_service.app.test_client()
        self.headers = {'X-Admin-Token': 'test-only-admin'}

    def save(self, provider):
        return self.client.put('/api/market/config', json={'provider': provider}, headers=self.headers)

    def test_management_routes_require_valid_token(self):
        for method in ('get', 'put'):
            for headers in ({}, {'X-Admin-Token': 'wrong'}):
                with self.subTest(method=method, headers=headers):
                    response = getattr(self.client, method)('/api/market/config', json={'provider': 'akshare'}, headers=headers)
                    self.assertEqual(response.status_code, 401)
        self.assertFalse(self.path.exists())
        with patch.dict(os.environ, {'GOLD_ADMIN_TOKEN': ''}):
            self.assertEqual(self.save('akshare').status_code, 503)

    def test_save_persists_and_new_requests_observe_provider_without_restart(self):
        self.assertEqual(self.client.get('/api/info').get_json()['data_provider'], 'direct')
        response = self.save('akshare')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['config']['source'], 'saved')
        self.assertEqual(json.loads(self.path.read_text())['provider'], 'akshare')
        self.assertEqual(self.client.get('/api/info').get_json()['data_provider'], 'akshare')
        config = self.client.get('/api/market/config', headers=self.headers).get_json()['config']
        self.assertEqual(config['provider'], 'akshare')
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_separate_scheduler_manager_reloads_shared_configuration(self):
        scheduler = market_config.MarketServiceManager(gold_service.GoldService, fallback='direct', path=self.path)
        old = scheduler.current()
        old._data_cache['previous'] = {'data': 'old'}
        self.assertEqual(self.save('akshare').status_code, 200)
        current = scheduler.current()
        self.assertEqual(current.data_provider, 'akshare')
        self.assertIsNot(current, old)
        self.assertEqual(current._data_cache, {})
        self.assertIs(scheduler.current(), current)

    def test_saved_provider_is_read_by_an_independent_process(self):
        self.save('akshare')
        script = '''
import sys
from types import SimpleNamespace
from market_config import MarketServiceManager
manager = MarketServiceManager(lambda provider: SimpleNamespace(data_provider=provider),
                               fallback='direct', path=sys.argv[1])
print(manager.current().data_provider)
'''
        result = subprocess.run([sys.executable, '-c', script, str(self.path)],
                                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'akshare')

    def test_switching_back_does_not_reuse_old_provider_cache(self):
        first = self.manager.current()
        first._data_cache['old'] = {'data': 'stale'}
        self.save('akshare')
        self.save('direct')
        current = self.manager.current()
        self.assertIsNot(current, first)
        self.assertEqual(current.data_provider, 'direct')
        self.assertEqual(current._data_cache, {})

    def test_invalid_changes_do_not_overwrite_saved_configuration(self):
        self.save('direct')
        original = self.path.read_bytes()
        for changes in (None, [], {}, {'provider': 1}, {'provider': None}, {'provider': 'auto'},
                        {'provider': 'akshare', 'extra': True}):
            with self.subTest(changes=changes):
                response = self.client.put('/api/market/config', json=changes, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.path.read_bytes(), original)

    def test_unavailable_sdk_preserves_working_configuration(self):
        old = self.manager.current()
        with patch.object(sge_client, '_load_akshare', side_effect=RuntimeError('SDK unavailable')):
            self.assertEqual(self.save('akshare').status_code, 503)
        self.assertFalse(self.path.exists())
        self.assertIs(self.manager.current(), old)

    def test_failed_atomic_write_preserves_config_and_cache(self):
        self.save('direct')
        old = self.manager.current()
        original = self.path.read_bytes()
        with patch('market_config.os.replace', side_effect=PermissionError('read only')):
            with self.assertLogs('gold_service', level='ERROR'):
                self.assertEqual(self.save('akshare').status_code, 500)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertIs(self.manager.current(), old)
        self.assertEqual(list(self.path.parent.glob('.market-data-*')), [])

    def test_corrupt_file_keeps_last_good_settings_and_logs_once(self):
        self.save('akshare')
        old = self.manager.current()
        self.path.write_text('{broken')
        with self.assertLogs('market_config', level='ERROR') as logs:
            self.assertIs(self.manager.current(), old)
            self.assertIs(self.manager.current(), old)
        self.assertEqual(len(logs.output), 1)
        self.assertTrue(self.manager.config()['warning'])
        self.save('direct')
        self.assertIsNone(self.manager.config()['warning'])

    def test_saved_settings_override_environment_and_survive_restart(self):
        self.save('akshare')
        restarted = market_config.MarketServiceManager(gold_service.GoldService, fallback='direct', path=self.path)
        self.assertEqual(restarted.current().data_provider, 'akshare')
        self.path.unlink()
        self.assertEqual(restarted.current().data_provider, 'direct')
        self.assertEqual(restarted.config()['source'], 'environment')

    def test_default_file_shares_the_existing_push_volume_directory(self):
        with patch.dict(os.environ, {'GOLD_PUSH_CONFIG_PATH': '/app/data/push_channels.json'}):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop('GOLD_MARKET_CONFIG_PATH', None)
                self.assertEqual(market_config.market_config_path(), Path('/app/data/market_data.json'))

    def test_request_snapshot_stays_consistent_while_new_requests_use_fresh_cache(self):
        started = threading.Event()
        release = threading.Event()
        calls = []

        def fetch(symbol, *, provider):
            calls.append(provider)
            if provider == 'direct':
                started.set()
                if not release.wait(3):
                    raise TimeoutError('test failed to release old request')
            return pd.DataFrame({'时间': [time(9)], '现价': [800 if provider == 'direct' else 810]})

        with patch.object(sge_client, 'spot_quotations_sge', side_effect=fetch):
            with ThreadPoolExecutor(max_workers=1) as pool:
                old_job = pool.submit(lambda: gold_service.app.test_client().get('/api/gold/spot_quotations_sge'))
                try:
                    self.assertTrue(started.wait(1))
                    self.assertEqual(self.save('akshare').status_code, 200)
                    new = self.client.get('/api/gold/spot_quotations_sge').get_json()
                    self.assertEqual(new['data_provider'], 'akshare')
                    self.assertEqual(new['data'][0]['现价'], 810)
                    release.set()
                    old = old_job.result(timeout=1).get_json()
                    self.assertEqual(old['data_provider'], 'direct')
                    self.assertEqual(old['data'][0]['现价'], 800)
                    cached = self.client.get('/api/gold/spot_quotations_sge').get_json()
                    self.assertEqual(cached['data_provider'], 'akshare')
                    self.assertEqual(cached['data'][0]['现价'], 810)
                    self.assertEqual(calls, ['direct', 'akshare'])
                finally:
                    release.set()


if __name__ == '__main__':
    unittest.main()
