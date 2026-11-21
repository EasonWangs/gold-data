"""Persist market-provider selection and retire caches safely on live changes."""

import json
import logging
import os
import tempfile
import threading
import uuid
from pathlib import Path

import sge_client
from push_service import runtime_config_path

logger = logging.getLogger(__name__)


def market_config_path():
    return Path(os.environ.get(
        'GOLD_MARKET_CONFIG_PATH', str(runtime_config_path().with_name('market_data.json')),
    ))


class MarketServiceManager:
    """One active service per process; all processes read the same atomic file.

    In-flight requests keep their existing service, including its caches. New
    requests get a fresh service after each saved revision, even for A→B→A.
    No network operation runs while this manager's lock is held.
    """

    def __init__(self, factory, *, fallback=None, path=None):
        self._factory = factory
        self._fallback = sge_client.get_provider(fallback)
        self._path = Path(path) if path is not None else market_config_path()
        self._lock = threading.RLock()
        self._service = None
        self._selection = None
        self._last_error = None

    def _read_selection(self):
        try:
            with self._path.open(encoding='utf-8') as file:
                saved = json.load(file)
        except FileNotFoundError:
            return self._fallback, None
        if (not isinstance(saved, dict) or not isinstance(saved.get('provider'), str)
                or not isinstance(saved.get('revision'), str) or not saved['revision']):
            raise ValueError('行情配置文件格式无效')
        return sge_client.get_provider(saved['provider']), saved['revision']

    def _refresh(self):
        try:
            selection = self._read_selection()
            if selection != self._selection:
                service = self._factory(provider=selection[0])
                self._service = service
                self._selection = selection
                logger.info('行情配置生效: provider=%s', selection[0])
            self._last_error = None
        except (OSError, ValueError, RuntimeError) as error:
            description = f'{type(error).__name__}: {error}'
            if description != self._last_error:
                logger.error('读取行情配置失败，保留最近有效配置: %s', description)
            self._last_error = description
            if self._service is None:
                self._service = self._factory(provider=self._fallback)
                self._selection = (self._fallback, None)
        return self._service

    def current(self):
        with self._lock:
            return self._refresh()

    def _public_config(self):
        return {
            'provider': self._service.data_provider,
            'source': 'saved' if self._selection[1] is not None else 'environment',
            'supported_providers': list(sge_client.SUPPORTED_PROVIDERS),
            'warning': '无法读取已保存配置，正在使用最近有效设置。' if self._last_error else None,
        }

    def config(self):
        with self._lock:
            self._refresh()
            return self._public_config()

    def update(self, changes):
        if not isinstance(changes, dict) or set(changes) != {'provider'}:
            raise ValueError('请求体必须是仅包含 provider 的 JSON 对象')
        if not isinstance(changes['provider'], str):
            raise ValueError('provider 必须是 direct 或 akshare')
        provider = sge_client.get_provider(changes['provider'])
        with self._lock:
            # Validate SDK availability before replacing a working configuration.
            service = self._factory(provider=provider)
            revision = uuid.uuid4().hex
            self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor, temporary_path = tempfile.mkstemp(
                prefix='.market-data-', suffix='.json', dir=self._path.parent, text=True,
            )
            try:
                with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
                    json.dump({'provider': provider, 'revision': revision}, file)
                    file.write('\n')
                    file.flush()
                    os.fsync(file.fileno())
                os.replace(temporary_path, self._path)
            finally:
                if os.path.exists(temporary_path):
                    os.unlink(temporary_path)
            self._service = service
            self._selection = (provider, revision)
            self._last_error = None
            logger.info('行情配置已保存: provider=%s', provider)
            return self._public_config()
