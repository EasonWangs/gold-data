"""SGE transport with explicit timeouts and the existing quote frame schema.

The AkShare entry points previously used here don't accept a timeout. Keep the
same SGE endpoints and parsing conventions without modifying Requests globally.
"""

import pandas as pd
import requests


SGE_BASE_URL = 'https://www.sge.com.cn'
# Connection timeout and socket read-idle timeout, NOT an overall deadline.
SGE_REQUEST_TIMEOUT = (3.05, 10)
SGE_HEADERS = {
    'Accept': 'application/json, text/javascript, */*; q=0.01',
    'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
    'Origin': SGE_BASE_URL,
    'Referer': f'{SGE_BASE_URL}/',
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36',
    'X-Requested-With': 'XMLHttpRequest',
}


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


def spot_quotations_sge(symbol):
    """Return minute quotes, excluding the source's not-yet-published rows."""
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


def spot_hist_sge(symbol):
    """Return the source's daily OHLC rows with date and numeric columns."""
    payload = _request_json('POST', '/graph/Dailyhq', symbol)
    history = pd.DataFrame(payload['time'], columns=['date', 'open', 'close', 'low', 'high'])
    history['date'] = pd.to_datetime(history['date'], errors='coerce').dt.date
    for column in ('open', 'close', 'low', 'high'):
        history[column] = pd.to_numeric(history[column], errors='coerce')
    return history
