"""Switchable direct/AKShare SGE transports with explicit network timeouts."""

import os
from functools import lru_cache
from importlib import import_module

import pandas as pd
import requests


SGE_BASE_URL = 'https://www.sge.com.cn'
# Connection timeout and socket read-idle timeout, NOT an overall deadline.
SGE_REQUEST_TIMEOUT = (3.05, 10)
PROVIDER_ENV = 'GOLD_SGE_PROVIDER'
SUPPORTED_PROVIDERS = ('direct', 'akshare')
SGE_HEADERS = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Origin': SGE_BASE_URL,
    'Referer': f'{SGE_BASE_URL}/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36',
    'X-Requested-With': 'XMLHttpRequest',
}


def get_provider(value=None):
    """Validate deployment configuration; never silently switch providers."""
    provider = (os.environ.get(PROVIDER_ENV, 'direct') if value is None else value).strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ValueError(f'{PROVIDER_ENV} 必须为 direct 或 akshare')
    return provider


@lru_cache(maxsize=1)
def _load_akshare():
    # Import only when selected: direct mode doesn't pay the SDK's startup cost.
    try:
        ak = import_module('akshare')
    except ImportError as error:
        raise RuntimeError('AkShare 未安装或依赖不完整；请安装 requirements.txt 中的依赖') from error
    required = ('set_timeout', 'spot_realtime_sge', 'spot_hist_sge')
    if not all(callable(getattr(ak, name, None)) for name in required):
        raise RuntimeError('AkShare 版本不兼容；请按 requirements.txt 安装 akshare==1.20.65')
    # The pinned SDK routes these endpoints through its own HTTP wrapper, which
    # reads this setting. This doesn't patch Requests or affect webhook timeouts.
    ak.set_timeout(SGE_REQUEST_TIMEOUT)
    return ak


def configure_provider(value=None):
    """Freeze one provider per service instance and fail early on bad installs."""
    provider = get_provider(value)
    if provider == 'akshare':
        _load_akshare()
    return provider


def _request_json(method, path, symbol):
    # Preserve the upstream's form-body convention, including for GET. Each
    # call owns its response/session; concurrent calls don't share mutable state.
    with requests.request(
        method,
        f'{SGE_BASE_URL}{path}',
        data={'instid': symbol},
        headers=SGE_HEADERS,
        timeout=SGE_REQUEST_TIMEOUT,
    ) as response:
        response.raise_for_status()
        return response.json()


def spot_quotations_sge(symbol, *, provider=None):
    """Return minute quotes, excluding the source's not-yet-published rows."""
    if get_provider(provider) == 'akshare':
        return _load_akshare().spot_realtime_sge(symbol=symbol)
    payload = _request_json('GET', '/graph/quotations', symbol)
    quotes = pd.DataFrame({
        '品种': payload['heyue'],
        '时间': payload['times'],
        '现价': payload['data'],
        '更新时间': payload['delaystr'],
    })
    if quotes.empty:
        return quotes
    quotes['现价'] = pd.to_numeric(quotes['现价'], errors='coerce')
    quotes['时间'] = pd.to_datetime(quotes['时间'], format='%H:%M').dt.time
    published_before = quotes['更新时间'].iloc[0].split()[1]
    quotes = quotes.loc[quotes['时间'].astype(str) < published_before]
    return quotes.sort_values('时间').reset_index(drop=True)


def spot_hist_sge(symbol, *, provider=None):
    """Return the source's daily OHLC rows with date and numeric columns."""
    if get_provider(provider) == 'akshare':
        return _load_akshare().spot_hist_sge(symbol=symbol)
    payload = _request_json('POST', '/graph/Dailyhq', symbol)
    history = pd.DataFrame(payload['time'], columns=['date', 'open', 'close', 'low', 'high'])
    history['date'] = pd.to_datetime(history['date'], errors='coerce').dt.date
    for column in ('open', 'close', 'low', 'high'):
        history[column] = pd.to_numeric(history[column], errors='coerce')
    return history
