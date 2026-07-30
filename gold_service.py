#!/usr/bin/env python3
"""
黄金价格服务
整合黄金价格API和钉钉推送功能的统一服务
"""

import akshare as ak
import pandas as pd
import requests
import json
import schedule
import time
import threading
import logging
import argparse
import hmac
import math
import os
from functools import wraps
from flask import Flask, jsonify, request
from datetime import datetime, time as clock_time, timedelta
from zoneinfo import ZoneInfo
from push_service import push_manager, MessageTemplate

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置 Jinja2 以避免与 Vue.js 语法冲突
app.jinja_env.variable_start_string = '[['
app.jinja_env.variable_end_string = ']]'

# 全局变量
scheduler_running = False
service_status = {
    'running': False,
    'start_time': None,
    'last_push': None,
    'push_count': 0,
    'errors': []
}

ADMIN_TOKEN_ENV = 'GOLD_ADMIN_TOKEN'
CORS_ALLOWED_ORIGINS_ENV = 'GOLD_CORS_ALLOWED_ORIGINS'
REALTIME_CACHE_TTL_SECONDS = 60
# 末尾报价相对前一有效报价的最大允许偏差（1.36%）。
REALTIME_TERMINAL_OUTLIER_MAX_CHANGE_RATIO = 0.0136
HISTORICAL_TRADING_CACHE_TTL_SECONDS = 30 * 60
HISTORICAL_OFF_HOURS_CACHE_TTL_SECONDS = 12 * 60 * 60
MARKET_TIMEZONE = ZoneInfo('Asia/Shanghai')
GOLD_SYMBOL = 'Au99.99'
SILVER_SYMBOL = 'Ag99.99'
GOLD_UNIT = '元/克'
SILVER_UNIT = '元/千克'
SUPPORTED_SMA_WINDOWS = (5, 10, 20, 30, 60)
DEFAULT_INDICATOR_DAYS = 60
MAX_INDICATOR_DAYS = 365
KDJ_RSV_WINDOW = 9
KDJ_SMOOTHING_PERIOD = 3
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
SUPPORTED_BACKTEST_STRATEGIES = {
    'ma5_20': {'label': 'MA5/20 交叉', 'basis': 'close', 'parameters': {'fast_period': 5, 'slow_period': 20}},
    'ma10_30': {'label': 'MA10/30 交叉', 'basis': 'close', 'parameters': {'fast_period': 10, 'slow_period': 30}},
    'trend_switch': {'label': 'MA30/60 趋势切换共振', 'basis': 'close', 'parameters': {'trend_fast_period': 30, 'trend_slow_period': 60, 'bull_strategy': 'ma5_20', 'bear_strategy': 'kdj'}},
    'macd': {'label': 'MACD DIF/DEA 交叉', 'basis': 'close', 'parameters': {'fast_period': 12, 'slow_period': 26, 'signal_period': 9}},
    'kdj': {'label': 'KDJ K/D 交叉', 'basis': 'high-low-close', 'parameters': {'rsv_window': 9, 'k_smoothing_period': 3, 'd_smoothing_period': 3}},
}
DEFAULT_BACKTEST_INITIAL_CASH = 100_000.0
DEFAULT_BACKTEST_ORDER_AMOUNT = 10_000.0
DEFAULT_BACKTEST_START_DATE = datetime(2025, 1, 1).date()
MIN_BACKTEST_INITIAL_CASH = 10_000.0
BACKTEST_INITIAL_CASH_STEP = 10_000.0
MIN_BACKTEST_ORDER_AMOUNT = 1_000.0
BACKTEST_ORDER_AMOUNT_STEP = 1_000.0
MAX_BACKTEST_CASH = 100_000_000.0
API_VERSION = '2.3.0'
DATA_SOURCE = '上海黄金交易所行情（经 AkShare 获取）'
SGE_SESSION_SCHEDULE = (
    {'name': 'night', 'label': '夜盘', 'time': '20:00–次日 02:30'},
    {'name': 'day_morning', 'label': '日盘上午', 'time': '09:00–11:30'},
    {'name': 'day_afternoon', 'label': '日盘下午', 'time': '13:30–15:30'},
)
SCHEDULED_PUSH_TIMES = {
    # Give the upstream minute feed time to publish the session's first quote.
    '09:02': ('day_opening', '日盘开盘', lambda: gold_service.push_opening_price()),
    '16:02': ('daily_settlement', '日线收盘', lambda: gold_service.push_closing_price()),
    '20:02': ('night_opening', '夜盘开盘', lambda: gold_service.push_night_opening_price()),
}
SCHEDULER_CONTROL_MESSAGE = (
    '定时推送仅能由独立调度进程管理；请使用 '
    '`python gold_service.py --scheduler-only` 启动。'
)
scheduled_pushes = set()


def get_allowed_cors_origins():
    """Return the explicitly configured browser origins allowed to call the API."""
    raw_origins = os.environ.get(CORS_ALLOWED_ORIGINS_ENV, '')
    return {
        origin.strip().rstrip('/')
        for origin in raw_origins.split(',')
        if origin.strip().startswith(('http://', 'https://'))
    }


@app.after_request
def add_configured_cors_headers(response):
    """Allow only configured cross-origin browser clients, never a wildcard."""
    origin = request.headers.get('Origin', '').rstrip('/')
    if origin not in get_allowed_cors_origins():
        return response

    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, X-Admin-Token'

    existing_vary = response.headers.get('Vary', '')
    if 'Origin' not in {value.strip() for value in existing_vary.split(',')}:
        response.headers['Vary'] = ', '.join(filter(None, (existing_vary, 'Origin')))
    return response


def market_now():
    """Return the current time in the Shanghai gold-market timezone."""
    return datetime.now(MARKET_TIMEZONE)


def market_timestamp():
    """Return an unambiguous ISO-8601 timestamp with the UTC+08:00 offset."""
    return market_now().isoformat(timespec='milliseconds')


def _next_weekday(value):
    """Return ``value`` moved forward to the next Monday–Friday date."""
    while value.weekday() >= 5:
        value += timedelta(days=1)
    return value


def get_sge_trading_day(moment=None):
    """Return the SGE trading-date label for a Shanghai-market moment.

    The night session opens on the prior calendar evening: Friday 20:00 and
    Saturday 01:00 therefore belong to the following Monday's trading day.
    This is a label for API consumers; official daily OHLC remains the source
    of truth for whether a public holiday is actually tradable.
    """
    moment = moment or market_now()
    moment = (
        moment.replace(tzinfo=MARKET_TIMEZONE)
        if moment.tzinfo is None else moment.astimezone(MARKET_TIMEZONE)
    )
    clock = moment.time()
    trading_date = moment.date()
    if clock >= clock_time(20):
        trading_date += timedelta(days=1)
    return _next_weekday(trading_date).isoformat()


def get_sge_session_status(moment=None):
    """Describe the current regular SGE session in a client-safe form."""
    moment = moment or market_now()
    moment = (
        moment.replace(tzinfo=MARKET_TIMEZONE)
        if moment.tzinfo is None else moment.astimezone(MARKET_TIMEZONE)
    )
    clock = moment.time()
    weekday = moment.weekday()
    if ((weekday < 5 and clock >= clock_time(20)) or
            (weekday in (1, 2, 3, 4, 5) and clock <= clock_time(2, 30))):
        session = 'night'
        label = '夜盘'
    elif weekday < 5 and clock_time(9) <= clock <= clock_time(11, 30):
        session = 'day_morning'
        label = '日盘上午'
    elif weekday < 5 and clock_time(13, 30) <= clock <= clock_time(15, 30):
        session = 'day_afternoon'
        label = '日盘下午'
    else:
        session = 'closed'
        label = '非连续竞价时段'

    return {
        'timezone': 'Asia/Shanghai',
        'market_time': moment.isoformat(timespec='milliseconds'),
        'trading_day': get_sge_trading_day(moment),
        'is_trading': session != 'closed',
        'session': session,
        'session_label': label,
        'regular_sessions': SGE_SESSION_SCHEDULE,
        'daily_bar_note': '一个交易日从前一自然日 20:00 的夜盘开始，并与其后日盘合并为同一根日线；法定节假日以源站实际行情为准。',
    }


def normalize_history_date(value):
    """Return the calendar date of a historical row's ``date`` field.

    The cached history frame carries either ISO strings or datetime-like
    values depending on the source path.  Both push routines compare that
    field against today, so they must interpret it identically.
    """
    if isinstance(value, str):
        return datetime.strptime(value, '%Y-%m-%d').date()
    return value.date() if hasattr(value, 'date') else value


def is_sge_trading_moment(moment):
    """Whether a Shanghai-clock datetime belongs to the regular SGE sessions."""
    clock = moment.time()
    weekday = moment.weekday()  # Monday is 0.
    return (
        (weekday in (1, 2, 3, 4, 5) and clock <= clock_time(2, 30)) or
        (weekday < 5 and (
            clock_time(9) <= clock <= clock_time(11, 30) or
            clock_time(13, 30) <= clock <= clock_time(15, 30)
        )) or
        (weekday < 5 and clock >= clock_time(20))
    )


def resolve_realtime_quote_datetime(value, market_time):
    """Restore the calendar date omitted by SGE's time-only realtime rows.

    A stale feed may be read outside trading hours, including weekends.  Rather
    than assuming every clock time belongs to today's calendar date, walk back
    to the most recent calendar date on which that clock time is a valid SGE
    session.  This keeps, for example, Friday 09:00 and 11:00 ticks on Friday
    when the same feed is read on Saturday morning.
    """
    candidate = datetime.combine(market_time.date(), value, tzinfo=MARKET_TIMEZONE)
    tolerance = timedelta(minutes=5) if is_sge_trading_moment(market_time) else timedelta()
    if candidate > market_time + tolerance:
        candidate -= timedelta(days=1)

    for _ in range(7):
        if is_sge_trading_moment(candidate):
            return candidate
        candidate -= timedelta(days=1)

    return None


def historical_cache_ttl_seconds(now=None):
    """Return the historical-data TTL for the current Shanghai market period.

    Daily historical bars can still change during the weekday day session, so
    they are refreshed every 30 minutes between 09:00 and 16:30.  Outside that
    window they are treated as settled and retained for 12 hours.
    """
    now = now or market_now()
    minutes_since_midnight = now.hour * 60 + now.minute
    is_weekday_day_session = (
        now.weekday() < 5
        and 9 * 60 <= minutes_since_midnight < 16 * 60 + 30
    )
    if is_weekday_day_session:
        return HISTORICAL_TRADING_CACHE_TTL_SECONDS
    return HISTORICAL_OFF_HOURS_CACHE_TTL_SECONDS


def drop_terminal_realtime_outlier(data, symbol):
    """Remove one implausible final tick from an upstream intraday series.

    SGE's upstream time series can occasionally end with an isolated placeholder
    quote.  The UI uses the last record as the latest price, so a terminal tick
    that differs by 1.36% or more from the preceding valid tick must not be
    exposed or cached.  The threshold is deliberately relative rather than a
    hard-coded price so it applies to both gold and silver.
    """
    if data is None or data.empty or '现价' not in data.columns or len(data) < 2:
        return data

    prices = pd.to_numeric(data['现价'], errors='coerce')
    valid_positions = [
        position
        for position, price in enumerate(prices)
        if pd.notna(price) and math.isfinite(float(price)) and price > 0
    ]
    if len(valid_positions) < 2:
        return data

    previous_position, latest_position = valid_positions[-2:]
    # Only change the terminal record.  A malformed earlier record is left
    # untouched because it cannot affect which price is presented as latest.
    if latest_position != len(data) - 1:
        return data

    previous_price = float(prices.iloc[previous_position])
    latest_price = float(prices.iloc[latest_position])
    change_ratio = abs(latest_price - previous_price) / previous_price
    at_outlier_threshold = (
        change_ratio > REALTIME_TERMINAL_OUTLIER_MAX_CHANGE_RATIO
        or math.isclose(
            change_ratio,
            REALTIME_TERMINAL_OUTLIER_MAX_CHANGE_RATIO,
            rel_tol=1e-12,
        )
    )
    if not at_outlier_threshold:
        return data

    logger.warning(
        '丢弃 %s 实时行情末尾异常报价: %.2f（上一有效报价 %.2f，偏差 %.2f%%）',
        symbol,
        latest_price,
        previous_price,
        change_ratio * 100,
    )
    return data.iloc[:-1].reset_index(drop=True)


def require_admin_token(view):
    """Protect management and manual-push endpoints with an environment token."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected_token = os.environ.get(ADMIN_TOKEN_ENV)
        provided_token = request.headers.get('X-Admin-Token', '')

        if not expected_token:
            logger.error('%s is not configured; rejecting management request', ADMIN_TOKEN_ENV)
            return jsonify({
                'status': 'error',
                'message': f'服务器未配置 {ADMIN_TOKEN_ENV}',
                'timestamp': market_timestamp()
            }), 503

        if not hmac.compare_digest(provided_token, expected_token):
            return jsonify({
                'status': 'error',
                'message': '管理令牌无效',
                'timestamp': market_timestamp()
            }), 401

        return view(*args, **kwargs)

    return wrapped

class GoldService:
    def __init__(self):
        config = self.load_dingtalk_config()
        self.webhook_url = config.get('webhook_url') if config else None
        self.link_url = config.get('link_url', 'http://127.0.0.1:5080') if config else 'http://127.0.0.1:5080'
        self.push_manager = push_manager
        self._data_cache = {}
        self._cache_lock = threading.Lock()

    def load_dingtalk_config(self):
        """加载钉钉配置"""
        try:
            with open('dingtalk_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config
        except FileNotFoundError:
            logger.warning("钉钉配置文件不存在")
            return None
        except Exception as e:
            logger.error(f"加载钉钉配置失败: {e}")
            return None

    def _get_cached_data(self, cache_key, loader, ttl_seconds, on_refresh=None, force_refresh=False):
        """Return fresh cached data and serialize cache misses per process."""
        now = time.monotonic()
        with self._cache_lock:
            cached = self._data_cache.get(cache_key)
            if (
                not force_refresh
                and
                cached
                and cached['ttl_seconds'] == ttl_seconds
                and now - cached['cached_at'] < ttl_seconds
            ):
                logger.debug('使用 %s 缓存数据', cache_key)
                return cached['data']

            data = loader()
            if data is not None:
                if on_refresh:
                    on_refresh()
                self._data_cache[cache_key] = {
                    'cached_at': time.monotonic(),
                    'ttl_seconds': ttl_seconds,
                    'data': data,
                }
            return data

    def get_real_time_sge_price(self, symbol):
        """获取指定上海黄金交易所品种的实时行情（缓存 60 秒）"""
        try:
            return self._get_cached_data(
                f'spot_quotations_sge:{symbol}',
                lambda: drop_terminal_realtime_outlier(
                    ak.spot_quotations_sge(symbol=symbol),
                    symbol,
                ),
                REALTIME_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            logger.error(f"获取 {symbol} 实时价格失败: {e}")
            return None

    def get_historical_sge_price(self, symbol, days=30, force_refresh=False):
        """获取历史数据；结算推送可跳过缓存以确认官方最新日线。"""
        try:
            spot_hist_sge_df = self._get_cached_data(
                f'spot_hist_sge:{symbol}',
                lambda: ak.spot_hist_sge(symbol=symbol),
                historical_cache_ttl_seconds(),
                on_refresh=lambda: self._evict_cached_indicators(symbol),
                force_refresh=force_refresh,
            )
            if spot_hist_sge_df is None:
                return None
            if days and days > 0:
                return spot_hist_sge_df.tail(days)
            return spot_hist_sge_df
        except Exception as e:
            logger.error(f"获取 {symbol} 历史价格失败: {e}")
            return None

    def get_cached_indicators(self, symbol, indicator_key, loader):
        """Cache derived daily indicators for the same period as history data."""
        return self._get_cached_data(
            f'indicators:{symbol}:{indicator_key}',
            loader,
            historical_cache_ttl_seconds(),
        )

    def _evict_cached_indicators(self, symbol):
        """Drop derived data when its cached history source is refreshed.

        This runs while ``_cache_lock`` is already held by ``_get_cached_data``.
        """
        prefix = f'indicators:{symbol}:'
        for cache_key in list(self._data_cache):
            if cache_key.startswith(prefix):
                del self._data_cache[cache_key]

    def get_real_time_gold_price(self):
        """获取 Au99.99 实时价格。"""
        return self.get_real_time_sge_price(GOLD_SYMBOL)

    def get_historical_gold_price(self, days=30, force_refresh=False):
        """获取 Au99.99 历史价格。"""
        return self.get_historical_sge_price(GOLD_SYMBOL, days, force_refresh=force_refresh)

    def get_gold_price_data(self):
        """获取完整黄金价格数据"""
        try:
            # 获取实时价格数据
            spot_data = self.get_real_time_gold_price()
            latest = None
            if spot_data is not None and not spot_data.empty:
                latest = spot_data.iloc[-1] if len(spot_data) > 0 else spot_data.iloc[0]

            # 获取历史数据
            hist_data = self.get_historical_gold_price()
            today_data = None
            prev_data = None
            if hist_data is not None and not hist_data.empty:
                today_data = hist_data.iloc[-1]  # 最新的历史数据（今日）
                prev_data = hist_data.iloc[-2] if len(hist_data) >= 2 else None  # 前一日数据

            return {
                'spot': latest,
                'history': today_data,
                'prev_history': prev_data
            }
        except Exception as e:
            logger.error(f"获取黄金价格数据失败: {e}")
            return None

    def send_message(self, message, service_name=None, **kwargs):
        """通过推送服务发送消息"""
        return self.push_manager.send_message(message, service_name, **kwargs)

    def is_trading_day(self):
        """Return whether the current calendar date can host a scheduled push.

        Weekday filtering is deliberately only a first guard.  Official daily
        data is checked again before a settlement message is delivered, so a
        public holiday never becomes a fabricated trading-day push.
        """
        return market_now().weekday() < 5

    def _record_successful_push(self):
        """Record only confirmed deliveries in the local scheduler status."""
        service_status['last_push'] = market_timestamp()
        service_status['push_count'] += 1

    @staticmethod
    def _quote_clock(value):
        """Normalise an upstream quote's time-only value, or return ``None``."""
        if isinstance(value, clock_time):
            return value
        if isinstance(value, str):
            try:
                return clock_time.fromisoformat(value)
            except ValueError:
                return None
        if hasattr(value, 'time'):
            return value.time()
        return None

    def _session_open_quote(self, spot_data, session_start):
        """Return the first quote near a session open without using stale ticks.

        The SGE realtime feed has a time-only column.  The scheduler runs two
        minutes after the open, and accepts the first quote published in the
        first five minutes rather than assuming a synthetic exact ``:00`` row.
        """
        if spot_data is None or spot_data.empty or '时间' not in spot_data.columns:
            return None, None

        end_minutes = session_start.hour * 60 + session_start.minute + 5
        candidates = []
        for _, row in spot_data.iterrows():
            quote_time = self._quote_clock(row.get('时间'))
            if quote_time is None:
                continue
            quote_minutes = quote_time.hour * 60 + quote_time.minute
            if session_start.hour <= quote_time.hour and (
                session_start.hour * 60 + session_start.minute <= quote_minutes <= end_minutes
            ):
                price = pd.to_numeric(pd.Series([row.get('现价')]), errors='coerce').iloc[0]
                if pd.notna(price) and math.isfinite(float(price)) and price > 0:
                    candidates.append((quote_time, float(price)))

        return min(candidates, default=(None, None), key=lambda item: item[0])

    def _previous_close_for_trading_day(self, trading_day):
        """Find the last completed official daily close before ``trading_day``."""
        history = self.get_historical_gold_price(days=None)
        if history is None or history.empty or 'close' not in history.columns:
            return None

        target = datetime.strptime(trading_day, '%Y-%m-%d').date()
        historical_dates = history['date'].map(normalize_history_date)
        completed = history.loc[historical_dates < target, 'close']
        if completed.empty:
            return None
        value = pd.to_numeric(pd.Series([completed.iloc[-1]]), errors='coerce').iloc[0]
        return float(value) if pd.notna(value) and math.isfinite(float(value)) else None

    def _push_session_opening_price(self, session_start, session_label):
        """Send a day- or night-session opening snapshot using one shared path."""
        if not self.is_trading_day():
            logger.info("今日为周末，跳过%s推送", session_label)
            return False, '今日非交易日，未发送开盘价格'

        logger.info("开始推送%s...", session_label)

        spot_data = self.get_real_time_gold_price()
        quote_time, open_price = self._session_open_quote(spot_data, session_start)
        if open_price is None:
            logger.error('未获取到 %s 前五分钟内的开盘报价，拒绝发送', session_label)
            return False, f'未获取到{session_label}开盘报价，未发送推送'

        status = get_sge_session_status()
        trading_day = status['trading_day']
        prev_close = self._previous_close_for_trading_day(trading_day)
        change = open_price - prev_close if prev_close is not None else 0
        change_percent = change / prev_close * 100 if prev_close else 0
        trend_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"
        message_data = {
            'date': trading_day,
            'time': quote_time.strftime('%H:%M:%S'),
            'session_label': session_label,
            'symbol': 'Au99.99（上海黄金交易所）',
            'open_price': open_price,
            'prev_close': f'{prev_close:.2f}' if prev_close is not None else 'N/A',
            'change': change,
            'change_percent': change_percent,
            'trend_emoji': trend_emoji,
            'link_url': self.link_url,
        }
        message = MessageTemplate.format_opening_price_message(message_data)
        sent = self.send_message(message, title=f'黄金{session_label}开盘播报')
        if sent:
            self._record_successful_push()
            return True, f'{session_label}开盘价格推送成功'

        logger.error('钉钉未接受%s开盘播报', session_label)
        return False, '钉钉未接受开盘价格推送'

    def push_opening_price(self):
        """推送日盘首个有效报价（09:00–09:05）。"""
        return self._push_session_opening_price(clock_time(9), '日盘')

    def push_night_opening_price(self):
        """推送夜盘首个有效报价（20:00–20:05）。"""
        return self._push_session_opening_price(clock_time(20), '夜盘')

    def push_closing_price(self, simulation=False):
        """Send a settlement message, or simulate it with the latest daily bar."""
        if not simulation and not self.is_trading_day():
            logger.info("今日为周末，跳过收盘价格推送")
            return False, '今日非交易日，未发送收盘价格'

        logger.info("开始%s收盘价格推送...", '模拟' if simulation else '正式')

        # 获取历史数据并检查日期
        # Settlement must not rely on a 30-minute intraday cache: the source
        # may publish the official daily bar shortly after the day session.
        hist_data_full = self.get_historical_gold_price(force_refresh=True)

        if hist_data_full is not None and not hist_data_full.empty:
            # 检查最后一条记录的日期
            last_record = hist_data_full.iloc[-1]

            # 转换日期进行比较
            record_date = normalize_history_date(last_record.get('date'))
            current_date = market_now().date()

            # 判断最后一条是否是今天的数据
            if record_date is not None and (record_date == current_date or simulation):
                # 官方日线已提供完整交易日（含夜盘）的 OHLC，不能用跨零点的
                # spot_quotations_sge 实时残片重新计算高低价。
                hist_data = last_record

                # 前一日数据是倒数第二条
                if len(hist_data_full) >= 2:
                    prev_data = hist_data_full.iloc[-2]
                    prev_close = prev_data.get('close', 0)
                else:
                    prev_close = 0
                if simulation and record_date != current_date:
                    logger.info('模拟推送使用最新已发布日线: %s（当前日期 %s）', record_date, current_date)
                else:
                    logger.info(f"使用今天的收盘数据: {record_date}")
            else:
                # 官方日线尚未更新时，实时行情会遗漏跨夜交易时段，不能据此拼接
                # 当日 OHLC 或高低价；宁可拒绝发送，也不能发布错误的收盘区间。
                logger.warning(f"历史数据最后一条是 {record_date},不是今天 {current_date}")
                error_data = {
                    'date': market_now().strftime('%Y-%m-%d'),
                    'time': market_now().strftime('%H:%M:%S'),
                    'link_url': self.link_url
                }
                error_msg = MessageTemplate.format_error_message(error_data)
                self.send_message(error_msg, title="数据获取失败")
                return False, '当日官方收盘数据尚未发布，未发送收盘价格'

            # 计算涨跌（相对于前一日收盘价）
            close_price = hist_data.get('close', 0)
            prev_close = prev_close

            # 涨跌额 = 今日收盘价 - 前一日收盘价
            change = close_price - prev_close if prev_close != 0 and close_price != 0 else 0
            # 涨跌幅 = (今日收盘价 - 前一日收盘价) / 前一日收盘价 * 100%
            change_percent = (change / prev_close * 100) if prev_close != 0 else 0

            # 涨跌表情
            trend_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"

            # 准备消息数据
            message_data = {
                'date': record_date.isoformat(),
                'time': market_now().strftime('%H:%M:%S'),
                'symbol': 'Au99.99 (上海黄金交易所)',
                'open_price': hist_data.get('open', 'N/A'),
                'close_price': hist_data.get('close', 'N/A'),
                'low_price': hist_data.get('low', 'N/A'),
                'high_price': hist_data.get('high', 'N/A'),
                'prev_close': f"{prev_close:.2f}" if prev_close != 0 else 'N/A',
                'trend_emoji': trend_emoji,
                'change': change,
                'change_percent': change_percent,
                'simulation_note': (
                    '> ⚠️ 此为手动模拟推送，展示最新已发布的官方日线。\n'
                    if simulation else ''
                ),
                'link_url': self.link_url
            }

            message = MessageTemplate.format_closing_price_message(message_data)
            title = '黄金收盘价格模拟播报' if simulation else '黄金收盘价格播报'
            sent = self.send_message(message, title=title)
            if sent:
                self._record_successful_push()
                strategy_result = self._push_closing_strategy_signals(
                    hist_data_full,
                    record_date,
                    close_price,
                    simulation,
                )
                message = '收盘价格模拟推送成功' if simulation else '收盘价格推送成功'
                if strategy_result is True:
                    return True, f'{message}；策略信号推送成功'
                if strategy_result is False:
                    return True, f'{message}；策略信号推送失败'
                return True, message

            logger.error('收盘价格推送未被钉钉接受')
            return False, '钉钉未接受收盘价格推送'
        else:
            error_data = {
                'date': market_now().strftime('%Y-%m-%d'),
                'time': market_now().strftime('%H:%M:%S'),
                'link_url': self.link_url
            }
            error_msg = MessageTemplate.format_error_message(error_data)
            self.send_message(error_msg, title="数据获取失败")
            return False, '无法获取收盘价格数据'

    def _push_closing_strategy_signals(self, history_data, trading_date, close_price, simulation):
        """Send an attention-grabbing, separate DingTalk message for close signals."""
        try:
            signals = collect_closing_strategy_signals(history_data, trading_date)
        except Exception:
            logger.exception('计算收盘策略信号失败，收盘行情快报已发送')
            return False

        if not signals:
            return None

        action_labels = {'buy': '买入', 'sell': '卖出'}
        action_emojis = {'buy': '🟢', 'sell': '🔴'}
        lines = []
        for signal in signals:
            lines.append(
                f"{action_emojis[signal['action']]} **{signal['strategy_name']}**："
                f"{action_labels[signal['action']]} · **{signal['signal_weight']}× 共振**"
            )
        message = MessageTemplate.format_strategy_signal_message({
            'date': trading_date.isoformat(),
            'close_price': float(close_price),
            'signals': '\n\n'.join(lines),
            'simulation_note': (
                '> ⚠️ 此为手动模拟推送，展示最新已发布的官方日线。\n'
                if simulation else ''
            ),
            'link_url': self.link_url,
        })
        title = '🚨 黄金策略信号（模拟）' if simulation else '🚨 黄金策略信号'
        sent = self.send_message(message, title=title)
        if sent:
            self._record_successful_push()
            return True

        logger.error('钉钉未接受收盘策略信号推送')
        return False

    def test_push(self):
        """测试推送功能"""
        logger.info("测试推送功能...")
        result = self.push_manager.test_service()
        if result:
            logger.info("✅ 推送测试成功")
            self._record_successful_push()
        else:
            logger.error("❌ 推送测试失败")
        return result

# 初始化服务
gold_service = GoldService()


def run_due_pushes():
    """Run each Shanghai-market scheduled push at most once per trading day."""
    now = market_now()
    if now.weekday() >= 5:
        return

    today_prefix = f'{now.date().isoformat()}:'
    scheduled_pushes.intersection_update(
        key for key in scheduled_pushes if key.startswith(today_prefix)
    )

    scheduled_push = SCHEDULED_PUSH_TIMES.get(now.strftime('%H:%M'))
    if not scheduled_push:
        return

    push_name, push_label, push_job = scheduled_push
    push_key = f'{now.date().isoformat()}:{push_name}'
    if push_key in scheduled_pushes:
        return

    # Record before invocation so a transient exception cannot create duplicate broadcasts.
    scheduled_pushes.add(push_key)
    logger.info('触发上海时区 %s 定时推送', push_label)
    push_job()


def run_scheduler_forever():
    """Run the scheduler in this dedicated process only."""
    global scheduler_running
    if scheduler_running:
        raise RuntimeError('调度器已在此进程中运行')

    schedule.clear('gold-price')
    # Only use an elapsed interval here. The due-time check itself is based on
    # Asia/Shanghai, so a UTC host or container cannot shift the push time.
    schedule.every(10).seconds.do(run_due_pushes).tag('gold-price')
    scheduler_running = True
    service_status['running'] = True
    service_status['start_time'] = market_timestamp()
    logger.info("📅 定时推送调度器已启动（Asia/Shanghai）")

    try:
        while scheduler_running:
            try:
                run_due_pushes()
                schedule.run_pending()
            except Exception as e:
                logger.exception(f"调度器运行异常: {e}")
                service_status['errors'].append({
                    'time': market_timestamp(),
                    'error': str(e)
                })
                # Keep error history bounded and prevent a failed due job from busy-looping.
                del service_status['errors'][:-100]
            time.sleep(10)
    finally:
        scheduler_running = False
        service_status['running'] = False
        schedule.clear('gold-price')


def scheduler_control_response():
    """Reject in-process scheduler control from WSGI workers."""
    return jsonify({
        'status': 'error',
        'message': SCHEDULER_CONTROL_MESSAGE,
        'timestamp': market_timestamp()
    }), 410


def push_response(success, message):
    """Return a truthful HTTP response for a manual push attempt."""
    if success:
        status_code = 200
    elif message.startswith('今日非交易日'):
        status_code = 409
    else:
        status_code = 502

    return jsonify({
        'status': 'success' if success else 'error',
        'message': message,
        'timestamp': market_timestamp()
    }), status_code


@app.route('/')
def index():
    """后台配置页。"""
    from flask import render_template
    return render_template('index.html')


@app.route('/backtest')
def backtest_page():
    """Independent server-side strategy backtest page."""
    from flask import render_template
    return render_template(
        'backtest.html',
        default_end_date=market_now().date().isoformat(),
    )

def serialize_sge_records(data, historical=False, market_time=None):
    """Convert SGE data frames to JSON-safe records.

    Realtime SGE rows only carry a clock time at the source.  The public API
    must retain the Shanghai date and UTC offset so consumers never have to
    infer a trading day from their own clock.
    """
    market_time = market_time or market_now()
    market_time = (
        market_time.replace(tzinfo=MARKET_TIMEZONE)
        if market_time.tzinfo is None
        else market_time.astimezone(MARKET_TIMEZONE)
    )
    serialized_records = []
    for record in data.to_dict('records'):
        discard_record = False
        for key, value in record.items():
            if not historical and key == '时间':
                if isinstance(value, str):
                    try:
                        value = clock_time.fromisoformat(value)
                    except ValueError:
                        discard_record = True
                        break

                if isinstance(value, clock_time):
                    quote_time = resolve_realtime_quote_datetime(value, market_time)
                    if quote_time is None:
                        discard_record = True
                        break
                    record[key] = quote_time.isoformat(timespec='seconds')
                    continue

            if hasattr(value, 'strftime'):
                if historical:
                    record[key] = (
                        value.strftime('%Y-%m-%dT%H:%M:%S.000')
                        if hasattr(value, 'hour')
                        else value.strftime('%Y-%m-%dT00:00:00.000')
                    )
                elif hasattr(value, 'hour'):
                    # pandas Timestamp / datetime values already carry a
                    # date.  Preserve it and make the market offset explicit.
                    if getattr(value, 'tzinfo', None) is None:
                        value = value.replace(tzinfo=MARKET_TIMEZONE)
                    else:
                        value = value.astimezone(MARKET_TIMEZONE)
                    record[key] = value.isoformat(timespec='seconds')
                else:
                    record[key] = value.strftime('%Y-%m-%d')
        if not discard_record:
            serialized_records.append(record)
    return serialized_records


def _clean_daily_history(data, value_columns, row_filter=None):
    """Shared cleaning skeleton for every daily-history indicator input.

    Trading dates are normalised and sorted ascending, the requested value
    columns are coerced to finite numbers, malformed rows are dropped, and a
    duplicated date keeps the last source record.

    Indicator-specific checks belong in ``row_filter`` rather than in extra
    parameters here: this function exists to hold the invariant every caller
    shares, not to become a switchboard for each indicator's semantics.
    """
    columns = ['date', *value_columns]
    if data is None or data.empty:
        return pd.DataFrame(columns=columns)

    missing_columns = set(columns).difference(data.columns)
    if missing_columns:
        raise ValueError(f"历史行情缺少 {'、'.join(sorted(missing_columns))} 字段")

    cleaned = data.loc[:, columns].copy()
    cleaned['date'] = pd.to_datetime(cleaned['date'], errors='coerce').dt.normalize()
    for column in value_columns:
        cleaned[column] = pd.to_numeric(cleaned[column], errors='coerce')
    cleaned = cleaned.dropna(subset=columns)
    cleaned = cleaned[cleaned[value_columns].map(math.isfinite).all(axis=1)]
    if row_filter is not None:
        cleaned = cleaned[row_filter(cleaned)]
    cleaned = cleaned.sort_values('date', kind='stable')
    cleaned = cleaned.drop_duplicates(subset=['date'], keep='last')
    return cleaned.reset_index(drop=True)


def clean_historical_close_data(data):
    """Return ordered, de-duplicated SGE daily dates and numeric close prices."""
    return _clean_daily_history(data, ['close'])


def calculate_sma_indicators(history_data, windows):
    """Calculate SMA columns on the complete cleaned daily-close history."""
    indicators = clean_historical_close_data(history_data)
    for window in windows:
        indicators[f'ma{window}'] = indicators['close'].rolling(
            window=window,
            min_periods=window,
        ).mean()
    return indicators


def parse_indicator_days():
    """Parse and strictly validate a common indicator days parameter."""
    days_values = request.args.getlist('days')

    if len(days_values) > 1:
        raise ValueError('days 参数只能提供一次')

    if days_values:
        try:
            days = int(days_values[0])
        except (TypeError, ValueError):
            raise ValueError('days 必须为正整数')
        if days <= 0 or days > MAX_INDICATOR_DAYS:
            raise ValueError(f'days 必须在 1 到 {MAX_INDICATOR_DAYS} 之间')
    else:
        days = DEFAULT_INDICATOR_DAYS

    return days


def parse_sma_indicator_parameters():
    """Parse and strictly validate SMA query parameters."""
    days = parse_indicator_days()
    windows_values = request.args.getlist('windows')

    if len(windows_values) > 1:
        raise ValueError('windows 参数只能提供一次')

    if windows_values:
        raw_windows = windows_values[0]
        if not raw_windows:
            raise ValueError('windows 不能为空')
        try:
            windows = [int(value.strip()) for value in raw_windows.split(',')]
        except (TypeError, ValueError):
            raise ValueError('windows 必须是以逗号分隔的整数')
        if not windows or any(window not in SUPPORTED_SMA_WINDOWS for window in windows):
            supported = ','.join(map(str, SUPPORTED_SMA_WINDOWS))
            raise ValueError(f'windows 仅支持 {supported}')
        if len(set(windows)) != len(windows):
            raise ValueError('windows 不能包含重复窗口')
    else:
        windows = list(SUPPORTED_SMA_WINDOWS)

    return days, windows


def _serialize_indicator_records(data, optional_fields=(), required_fields=()):
    """Shared shape for indicator responses: ``date``, ``close``, then values.

    ``optional_fields`` serialise NaN as null — the leading rows of a rolling
    window legitimately have no value yet.  ``required_fields`` are always
    numeric; a NaN there means the calculation is broken and should surface
    rather than be silently nulled.
    """
    records = []
    for _, row in data.iterrows():
        record = {
            'date': row['date'].strftime('%Y-%m-%d'),
            'close': float(row['close']),
        }
        for field in optional_fields:
            value = row[field]
            record[field] = None if pd.isna(value) else float(value)
        for field in required_fields:
            record[field] = float(row[field])
        records.append(record)
    return records


def serialize_sma_records(data, windows):
    """Convert a calculated SMA data frame into the public response shape."""
    return _serialize_indicator_records(
        data, optional_fields=[f'ma{window}' for window in windows]
    )


def sma_indicator_response(symbol, metal_name, unit):
    """Build a daily-close SMA indicator response from cached SGE history."""
    try:
        days, windows = parse_sma_indicator_parameters()
    except ValueError as error:
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 400

    try:
        # Request the full cached series first; calculating after a source slice
        # would make the first response row's longer SMAs incomplete.
        history_data = gold_service.get_historical_sge_price(symbol, days=None)
        indicator_key = f"sma:{','.join(map(str, windows))}"
        indicators = gold_service.get_cached_indicators(
            symbol,
            indicator_key,
            lambda: calculate_sma_indicators(history_data, windows),
        )
        if indicators.empty:
            return jsonify({
                'status': 'error',
                'message': f'无法获取有效的历史{metal_name}收盘价数据',
                'timestamp': market_timestamp(),
            }), 500

        response_data = serialize_sma_records(indicators.tail(days), windows)
        latest = response_data[-1]
        return jsonify({
            'status': 'success',
            'timestamp': market_timestamp(),
            'symbol': symbol,
            'unit': unit,
            'basis': 'close',
            'windows': windows,
            'data': response_data,
            'latest': latest,
            'count': len(response_data),
            'available_history_count': len(indicators),
        })
    except ValueError as error:
        logger.error('计算 %s SMA 指标失败: %s', symbol, error)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500
    except Exception as error:
        logger.exception('计算 %s SMA 指标失败', symbol)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500


def clean_historical_ohlc_data(data):
    """Return ordered valid daily high-low-close data for range indicators.

    Adds the one check the close-only history cannot express: a row whose high
    is below its low is malformed and must not reach a range calculation.
    """
    return _clean_daily_history(
        data,
        ['high', 'low', 'close'],
        row_filter=lambda frame: frame['high'] >= frame['low'],
    )


def calculate_kdj_indicators(history_data):
    """Calculate daily KDJ with 9-day RSV and K/D initial values of 50."""
    indicators = clean_historical_ohlc_data(history_data)
    low_n = indicators['low'].rolling(KDJ_RSV_WINDOW, min_periods=KDJ_RSV_WINDOW).min()
    high_n = indicators['high'].rolling(KDJ_RSV_WINDOW, min_periods=KDJ_RSV_WINDOW).max()
    price_range = high_n - low_n
    rsv = ((indicators['close'] - low_n) / price_range * 100).where(price_range != 0, 50.0)

    k_values = []
    d_values = []
    previous_k = 50.0
    previous_d = 50.0
    smoothing_weight = KDJ_SMOOTHING_PERIOD - 1
    for value in rsv:
        if pd.isna(value):
            k_values.append(float('nan'))
            d_values.append(float('nan'))
            continue
        current_k = (smoothing_weight * previous_k + value) / KDJ_SMOOTHING_PERIOD
        current_d = (smoothing_weight * previous_d + current_k) / KDJ_SMOOTHING_PERIOD
        k_values.append(current_k)
        d_values.append(current_d)
        previous_k = current_k
        previous_d = current_d

    indicators['k'] = k_values
    indicators['d'] = d_values
    indicators['j'] = 3 * indicators['k'] - 2 * indicators['d']
    return indicators


def serialize_kdj_records(data):
    """Convert calculated KDJ rows into JSON-safe public API records."""
    return _serialize_indicator_records(data, optional_fields=('k', 'd', 'j'))


def kdj_indicator_response(symbol, metal_name, unit):
    """Build a daily high-low-close KDJ response from cached SGE history."""
    try:
        days = parse_indicator_days()
    except ValueError as error:
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 400

    try:
        history_data = gold_service.get_historical_sge_price(symbol, days=None)
        indicators = gold_service.get_cached_indicators(
            symbol,
            'kdj:9,3,3',
            lambda: calculate_kdj_indicators(history_data),
        )
        if indicators.empty:
            return jsonify({
                'status': 'error',
                'message': f'无法获取有效的历史{metal_name}高低收盘价数据',
                'timestamp': market_timestamp(),
            }), 500

        response_data = serialize_kdj_records(indicators.tail(days))
        return jsonify({
            'status': 'success',
            'timestamp': market_timestamp(),
            'symbol': symbol,
            'unit': unit,
            'basis': 'high-low-close',
            'parameters': {
                'rsv_window': KDJ_RSV_WINDOW,
                'k_smoothing_period': KDJ_SMOOTHING_PERIOD,
                'd_smoothing_period': KDJ_SMOOTHING_PERIOD,
                'initial_k': 50,
                'initial_d': 50,
            },
            'data': response_data,
            'latest': response_data[-1],
            'count': len(response_data),
            'available_history_count': len(indicators),
        })
    except ValueError as error:
        logger.error('计算 %s KDJ 指标失败: %s', symbol, error)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500
    except Exception as error:
        logger.exception('计算 %s KDJ 指标失败', symbol)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500


def calculate_macd_indicators(history_data):
    """Calculate daily MACD DIF and DEA from the complete close-price history.

    EMA values use the first valid close as their initial value (pandas'
    ``adjust=False`` convention), so DIF and DEA are available from the first
    trading day and remain consistent when a later response is sliced.
    """
    indicators = clean_historical_close_data(history_data)
    fast_ema = indicators['close'].ewm(span=MACD_FAST_PERIOD, adjust=False).mean()
    slow_ema = indicators['close'].ewm(span=MACD_SLOW_PERIOD, adjust=False).mean()
    indicators['dif'] = fast_ema - slow_ema
    indicators['dea'] = indicators['dif'].ewm(
        span=MACD_SIGNAL_PERIOD,
        adjust=False,
    ).mean()
    return indicators


def serialize_macd_records(data):
    """Convert calculated MACD rows into JSON-safe public API records."""
    return _serialize_indicator_records(data, required_fields=('dif', 'dea'))


def macd_indicator_response(symbol, metal_name, unit):
    """Build a daily-close MACD response from cached SGE history."""
    try:
        days = parse_indicator_days()
    except ValueError as error:
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 400

    try:
        history_data = gold_service.get_historical_sge_price(symbol, days=None)
        indicators = gold_service.get_cached_indicators(
            symbol,
            'macd:12,26,9',
            lambda: calculate_macd_indicators(history_data),
        )
        if indicators.empty:
            return jsonify({
                'status': 'error',
                'message': f'无法获取有效的历史{metal_name}收盘价数据',
                'timestamp': market_timestamp(),
            }), 500

        response_data = serialize_macd_records(indicators.tail(days))
        return jsonify({
            'status': 'success',
            'timestamp': market_timestamp(),
            'symbol': symbol,
            'unit': unit,
            'basis': 'close',
            'parameters': {
                'fast_period': MACD_FAST_PERIOD,
                'slow_period': MACD_SLOW_PERIOD,
                'signal_period': MACD_SIGNAL_PERIOD,
            },
            'data': response_data,
            'latest': response_data[-1],
            'count': len(response_data),
            'available_history_count': len(indicators),
        })
    except ValueError as error:
        logger.error('计算 %s MACD 指标失败: %s', symbol, error)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500
    except Exception as error:
        logger.exception('计算 %s MACD 指标失败', symbol)
        return jsonify({
            'status': 'error',
            'message': str(error),
            'timestamp': market_timestamp(),
        }), 500


def _parse_backtest_date(name):
    """Parse an optional ISO date query parameter into a calendar date."""
    value = request.args.get(name)
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except ValueError:
        raise ValueError(f'{name} 必须为 YYYY-MM-DD 日期')


def _parse_backtest_money(name, default, minimum, step):
    """Parse money using the same lower bound and increment as the UI."""
    value = request.args.get(name)
    if value is None or value == '':
        return default
    try:
        amount = float(value)
    except ValueError:
        raise ValueError(f'{name} 必须为正数')
    if not math.isfinite(amount) or amount < minimum or amount > MAX_BACKTEST_CASH:
        raise ValueError(f'{name} 必须在 {minimum:,.0f} 到 {MAX_BACKTEST_CASH:,.0f} 之间')
    if not math.isclose(amount / step, round(amount / step), rel_tol=0, abs_tol=1e-9):
        raise ValueError(f'{name} 必须为 {step:,.0f} 的整数倍')
    return round(amount, 2)


def parse_backtest_parameters():
    """Validate public backtest query parameters without silent coercion."""
    strategy = request.args.get('strategy', 'ma5_20')
    if strategy not in SUPPORTED_BACKTEST_STRATEGIES:
        supported = ', '.join(SUPPORTED_BACKTEST_STRATEGIES)
        raise ValueError(f'strategy 仅支持 {supported}')

    start_date = _parse_backtest_date('start') or DEFAULT_BACKTEST_START_DATE
    end_date = _parse_backtest_date('end') or market_now().date()
    if start_date and end_date and start_date > end_date:
        raise ValueError('start 不能晚于 end')

    initial_cash = _parse_backtest_money(
        'initial_cash',
        DEFAULT_BACKTEST_INITIAL_CASH,
        MIN_BACKTEST_INITIAL_CASH,
        BACKTEST_INITIAL_CASH_STEP,
    )
    order_amount = _parse_backtest_money(
        'order_amount',
        DEFAULT_BACKTEST_ORDER_AMOUNT,
        MIN_BACKTEST_ORDER_AMOUNT,
        BACKTEST_ORDER_AMOUNT_STEP,
    )
    if order_amount > initial_cash:
        raise ValueError('order_amount 不能高于 initial_cash')

    return {
        'strategy': strategy,
        'start_date': start_date,
        'end_date': end_date,
        'initial_cash': initial_cash,
        'order_amount': order_amount,
    }


def build_backtest_indicators(history_data):
    """Build one aligned daily frame for all server-side strategy signals."""
    indicators = clean_historical_ohlc_data(history_data)
    if indicators.empty:
        return indicators

    for window in SUPPORTED_SMA_WINDOWS:
        indicators[f'ma{window}'] = indicators['close'].rolling(
            window=window,
            min_periods=window,
        ).mean()

    macd = calculate_macd_indicators(indicators)
    kdj = calculate_kdj_indicators(indicators)
    indicators['dif'] = macd['dif'].to_numpy()
    indicators['dea'] = macd['dea'].to_numpy()
    indicators['k'] = kdj['k'].to_numpy()
    indicators['d'] = kdj['d'].to_numpy()
    return indicators


def _backtest_resonance(current, action, primary_strategy):
    """Return the front-end-aligned 1x–4x signal weight and confirmations."""
    expected_above = action == 'buy'
    confirmations = []

    def is_above(left, right):
        if pd.isna(left) or pd.isna(right):
            return False
        return left > right if expected_above else left < right

    if primary_strategy != 'ma5_20' and is_above(current['ma5'], current['ma20']):
        confirmations.append('ma5_20')
    if primary_strategy != 'ma10_30' and is_above(current['ma10'], current['ma30']):
        confirmations.append('ma10_30')
    if primary_strategy != 'macd':
        dif, dea = current['dif'], current['dea']
        macd_confirmed = (
            not pd.isna(dif) and not pd.isna(dea)
            and (dif > dea and dif > 0 if expected_above else dif < dea)
        )
        if macd_confirmed:
            confirmations.append('macd')
    if primary_strategy != 'kdj' and is_above(current['k'], current['d']):
        confirmations.append('kdj')

    return 1 + len(confirmations), confirmations


def _backtest_signal(indicators, index, strategy):
    """Return a confirmed crossover signal for a row, or ``None``."""
    if index == 0:
        return None
    current = indicators.iloc[index]
    previous = indicators.iloc[index - 1]

    if strategy == 'trend_switch':
        trend_values = (current['ma30'], current['ma60'])
        if any(pd.isna(value) for value in trend_values):
            return None
        active_strategy = 'ma5_20' if current['ma30'] > current['ma60'] else 'kdj' if current['ma30'] < current['ma60'] else None
        if active_strategy is None:
            return None
        signal = _backtest_signal(indicators, index, active_strategy)
        if signal is None:
            return None
        regime = 'MA30 位于 MA60 上方，采用 MA5/20 共振' if active_strategy == 'ma5_20' else 'MA30 位于 MA60 下方，采用 KDJ 共振'
        signal['reason'] = f'{regime}；{signal["reason"]}'
        signal['indicators'] = {
            **signal['indicators'],
            'ma30': current['ma30'],
            'ma60': current['ma60'],
        }
        return signal
    if strategy in ('ma5_20', 'ma10_30'):
        fast_period, slow_period = (
            (5, 20) if strategy == 'ma5_20' else (10, 30)
        )
        fast_key, slow_key = f'ma{fast_period}', f'ma{slow_period}'
        values = (previous[fast_key], previous[slow_key], current[fast_key], current[slow_key])
        if any(pd.isna(value) for value in values):
            return None
        previous_difference = previous[fast_key] - previous[slow_key]
        current_difference = current[fast_key] - current[slow_key]
        if previous_difference <= 0 and current_difference > 0:
            action, reason = 'buy', f'MA{fast_period} 上穿 MA{slow_period}'
        elif previous_difference >= 0 and current_difference < 0:
            action, reason = 'sell', f'MA{fast_period} 下穿 MA{slow_period}'
        else:
            return None
        signal_indicators = {fast_key: current[fast_key], slow_key: current[slow_key]}
    elif strategy == 'macd':
        values = (previous['dif'], previous['dea'], current['dif'], current['dea'])
        if any(pd.isna(value) for value in values):
            return None
        if (previous['dif'] <= previous['dea'] and current['dif'] > current['dea']
                and current['dif'] > 0 and current['dea'] > 0):
            action, reason = 'buy', 'DIF 上穿 DEA（零轴上方）'
        elif previous['dif'] >= previous['dea'] and current['dif'] < current['dea']:
            action, reason = 'sell', 'DIF 下穿 DEA'
        else:
            return None
        signal_indicators = {'dif': current['dif'], 'dea': current['dea']}
    else:
        values = (previous['k'], previous['d'], current['k'], current['d'])
        if any(pd.isna(value) for value in values):
            return None
        if previous['k'] <= previous['d'] and current['k'] > current['d']:
            action, reason = 'buy', 'K 上穿 D'
        elif previous['k'] >= previous['d'] and current['k'] < current['d']:
            action, reason = 'sell', 'K 下穿 D'
        else:
            return None
        signal_indicators = {'k': current['k'], 'd': current['d']}

    signal_weight, confirmations = _backtest_resonance(current, action, strategy)
    return {
        'action': action,
        'reason': reason,
        'indicators': signal_indicators,
        'signal_weight': signal_weight,
        'confirmations': confirmations,
    }


def _serialise_backtest_indicators(values):
    return {
        name: round(float(value), 4)
        for name, value in values.items()
    }


def collect_closing_strategy_signals(history_data, trading_date):
    """Collect all crossover signals for one published daily bar.

    The closing push uses the official daily bar that it is about to report,
    so strategy signals and the published close always refer to the same SGE
    trading day.  A primary crossover can yield one result for each strategy.
    """
    indicators = build_backtest_indicators(history_data)
    if indicators.empty:
        return []

    target_date = normalize_history_date(trading_date)
    matches = indicators.index[indicators['date'].dt.date == target_date]
    if matches.empty:
        return []

    index = indicators.index.get_loc(matches[-1])
    signals = []
    for strategy, definition in SUPPORTED_BACKTEST_STRATEGIES.items():
        signal = _backtest_signal(indicators, index, strategy)
        if signal is None:
            continue
        signals.append({
            'strategy': strategy,
            'strategy_name': definition['label'],
            'action': signal['action'],
            'signal_weight': signal['signal_weight'],
            'confirmations': signal['confirmations'],
        })
    return signals


def run_strategy_backtest(history_data, strategy='ma5_20', start_date=None, end_date=None,
                          initial_cash=DEFAULT_BACKTEST_INITIAL_CASH,
                          order_amount=DEFAULT_BACKTEST_ORDER_AMOUNT):
    """Simulate resonance-weighted daily-close crossover trades from SGE bars.

    This is an auditable research simulation, not an order-execution engine.
    Indicators are calculated from the full clean history before the requested
    period is selected, so a short request window still receives valid MA30,
    MACD, and KDJ warm-up data.
    """
    if strategy not in SUPPORTED_BACKTEST_STRATEGIES:
        raise ValueError(f'未知策略: {strategy}')

    indicators = build_backtest_indicators(history_data)
    if indicators.empty:
        raise ValueError('没有可用于回测的完整日线数据')

    selected = indicators.copy()
    if start_date:
        selected = selected[selected['date'].dt.date >= start_date]
    if end_date:
        selected = selected[selected['date'].dt.date <= end_date]
    selected = selected.reset_index(drop=True)
    if selected.empty:
        raise ValueError('所选日期区间没有可用日线数据')

    cash = float(initial_cash)
    position = 0.0
    position_cost = 0.0
    trades = []
    latest_signal = None
    first_date = selected.iloc[0]['date']
    last_date = selected.iloc[-1]['date']

    # The selection must retain its predecessor from the complete history so
    # a cross on the first requested date is not accidentally hidden.
    selected_dates = set(selected['date'])
    for index, row in indicators.iterrows():
        if row['date'] not in selected_dates:
            continue
        signal = _backtest_signal(indicators, index, strategy)
        if signal is None:
            continue

        price = float(row['close'])
        date_text = row['date'].strftime('%Y-%m-%d')
        base_order_amount = float(order_amount)
        requested_amount = base_order_amount * signal['signal_weight']
        # Keep buy orders atomic: a short cash balance is an auditable failed
        # signal, never an implicit smaller purchase.
        amount = requested_amount if signal['action'] == 'buy' else min(requested_amount, position * price)
        execution_status = 'executed'
        quantity = 0.0
        reason = signal['reason']
        if signal['action'] == 'buy' and cash >= amount:
            quantity = amount / price
            cash -= amount
            position += quantity
            position_cost += amount
        elif signal['action'] == 'sell' and amount > 0 and position > 0:
            quantity = amount / price
            quantity = min(quantity, position)
            amount = quantity * price
            cost = position_cost * quantity / position
            cash += amount
            position -= quantity
            position_cost -= cost
        else:
            execution_status = 'skipped'
            reason = f'{reason}；' + ('可用资金不足' if signal['action'] == 'buy' else '当前无持仓')

        trade = {
            'date': date_text,
            'action': signal['action'],
            'execution_status': execution_status,
            'price': round(price, 2),
            'amount': round(amount, 2),
            'base_order_amount': round(base_order_amount, 2),
            'signal_weight': signal['signal_weight'],
            'confirmations': signal['confirmations'],
            'quantity': round(quantity, 6),
            'position_after': round(position, 6),
            'cash_after': round(cash, 2),
            'reason': reason,
            'indicators': _serialise_backtest_indicators(signal['indicators']),
        }
        trades.append(trade)
        latest_signal = {
            key: trade[key]
            for key in (
                'date', 'action', 'execution_status', 'reason', 'indicators',
                'signal_weight', 'confirmations',
            )
        }

    latest_price = float(selected.iloc[-1]['close'])
    position_value = position * latest_price
    total_value = cash + position_value
    profit = total_value - initial_cash
    elapsed_days = max((last_date - first_date).days, 0)
    annualized_return = 0.0
    if elapsed_days > 0 and total_value > 0:
        annualized_return = (math.pow(total_value / initial_cash, 365.25 / elapsed_days) - 1) * 100
    if latest_signal is None or latest_signal['date'] != last_date.strftime('%Y-%m-%d'):
        latest_signal = {
            'date': last_date.strftime('%Y-%m-%d'),
            'action': 'hold',
            'execution_status': 'not_triggered',
            'reason': '最新交易日未出现该策略的交叉信号',
            'indicators': {},
            'signal_weight': 0,
            'confirmations': [],
        }

    executed_trades = [trade for trade in trades if trade['execution_status'] == 'executed']
    return {
        'strategy': strategy,
        'strategy_name': SUPPORTED_BACKTEST_STRATEGIES[strategy]['label'],
        'parameters': SUPPORTED_BACKTEST_STRATEGIES[strategy]['parameters'],
        'assumptions': {
            'execution_price': '信号日收盘价',
            'order_amount': round(float(order_amount), 2),
            'resonance': {
                'base_weight': 1,
                'maximum_weight': 4,
                'description': '主交叉触发为 1x，另外三个同向指标每项增加 1x。',
            },
            'fees_included': False,
            'slippage_included': False,
            'note': '回测仅用于历史研究，不构成投资建议或实际交易指令。',
        },
        'period': {'start': first_date.strftime('%Y-%m-%d'), 'end': last_date.strftime('%Y-%m-%d'), 'trading_days': len(selected)},
        'summary': {
            'initial_cash': round(float(initial_cash), 2),
            'cash': round(cash, 2),
            'position': round(position, 6),
            'position_value': round(position_value, 2),
            'latest_price': round(latest_price, 2),
            'total_value': round(total_value, 2),
            'profit': round(profit, 2),
            'return_rate': round(profit / initial_cash * 100, 2),
            'annualized_return': round(annualized_return, 2),
            'executed_trade_count': len(executed_trades),
            'skipped_signal_count': len(trades) - len(executed_trades),
        },
        'latest_signal': latest_signal,
        'trades': trades,
    }


def backtest_response(symbol, metal_name, unit):
    """Build the public server-side strategy backtest response."""
    try:
        options = parse_backtest_parameters()
    except ValueError as error:
        return jsonify({'status': 'error', 'message': str(error), 'timestamp': market_timestamp()}), 400

    try:
        history_data = gold_service.get_historical_sge_price(symbol, days=None)
        result = run_strategy_backtest(history_data, **options)
        return jsonify({
            'status': 'success',
            'timestamp': market_timestamp(),
            'symbol': symbol,
            'unit': unit,
            'data_source': DATA_SOURCE,
            'market': get_sge_session_status(),
            'result': result,
        })
    except ValueError as error:
        return jsonify({'status': 'error', 'message': str(error), 'timestamp': market_timestamp()}), 422
    except Exception as error:
        logger.exception('计算 %s 策略回测失败', metal_name)
        return jsonify({'status': 'error', 'message': str(error), 'timestamp': market_timestamp()}), 500


def realtime_sge_response(symbol, metal_name):
    """Build a realtime SGE API response for a metal symbol."""
    try:
        data = gold_service.get_real_time_sge_price(symbol)
        if data is not None and not data.empty:
            response_time = market_now()
            # Rows whose clock time cannot be placed on a trading day are
            # dropped by the serializer, so count must describe what is
            # actually returned rather than the raw upstream length.
            records = serialize_sge_records(data, market_time=response_time)
            discarded = len(data) - len(records)
            if discarded:
                logger.warning(
                    '%s 实时行情丢弃 %d 条无法归入交易时段的报价（上游 %d 条）',
                    symbol, discarded, len(data),
                )
            result = {
                "status": "success",
                "timestamp": response_time.isoformat(timespec='milliseconds'),
                "symbol": symbol,
                "unit": GOLD_UNIT if symbol == GOLD_SYMBOL else SILVER_UNIT,
                "data_source": DATA_SOURCE,
                "market": get_sge_session_status(response_time),
                "data": records,
                "count": len(records)
            }
            return jsonify(result)
        else:
            return jsonify({
                "status": "error",
                "message": f"无法获取实时{metal_name}价格数据",
                "timestamp": market_timestamp()
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": market_timestamp()
        }), 500


def historical_sge_response(symbol, metal_name):
    """Build a historical SGE API response for a metal symbol."""
    try:
        raw_days = request.args.get('days')
        if raw_days is None or raw_days == '':
            days = None
        else:
            try:
                days = int(raw_days)
            except ValueError:
                raise ValueError('days 必须为正整数')
            if days <= 0:
                raise ValueError('days 必须为正整数')
    except ValueError as error:
        return jsonify({
            "status": "error",
            "message": str(error),
            "timestamp": market_timestamp()
        }), 400

    try:
        data = gold_service.get_historical_sge_price(symbol, days)
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": market_timestamp()
        }), 500

    if data is None or data.empty:
        return jsonify({
            "status": "error",
            "message": f"无法获取历史{metal_name}价格数据",
            "timestamp": market_timestamp()
        }), 500

    return jsonify({
        "status": "success",
        "timestamp": market_timestamp(),
        "symbol": symbol,
        "unit": GOLD_UNIT if symbol == GOLD_SYMBOL else SILVER_UNIT,
        "data_source": DATA_SOURCE,
        "market": get_sge_session_status(),
        "data": serialize_sge_records(data, historical=True),
        "count": len(data),
        "days": days or "all",
        "daily_bar_note": '历史日线按上金所交易日聚合，包含该交易日前一自然日夜盘与其后日盘。'
    })


# 黄金与白银价格 API
@app.route('/api/gold/spot_quotations_sge', methods=['GET'])
def api_realtime_gold_price():
    """实时黄金价格 API 接口。"""
    return realtime_sge_response(GOLD_SYMBOL, '黄金')


@app.route('/api/gold/spot_hist_sge', methods=['GET'])
def api_historical_gold_price():
    """历史黄金价格 API 接口。"""
    return historical_sge_response(GOLD_SYMBOL, '黄金')


@app.route('/api/gold/indicators/sma', methods=['GET'])
def api_gold_sma_indicators():
    """Au99.99 daily-close simple moving averages."""
    return sma_indicator_response(GOLD_SYMBOL, '黄金', GOLD_UNIT)


@app.route('/api/gold/indicators/kdj', methods=['GET'])
def api_gold_kdj_indicators():
    """Au99.99 daily high-low-close KDJ indicators."""
    return kdj_indicator_response(GOLD_SYMBOL, '黄金', GOLD_UNIT)


@app.route('/api/gold/indicators/macd', methods=['GET'])
def api_gold_macd_indicators():
    """Au99.99 daily-close MACD DIF and DEA indicators."""
    return macd_indicator_response(GOLD_SYMBOL, '黄金', GOLD_UNIT)


@app.route('/api/gold/backtest', methods=['GET'])
def api_gold_backtest():
    """Server-side daily-bar strategy backtest for Au99.99."""
    return backtest_response(GOLD_SYMBOL, '黄金', GOLD_UNIT)


@app.route('/api/silver/spot_quotations_sge', methods=['GET'])
def api_realtime_silver_price():
    """实时白银价格 API 接口。"""
    return realtime_sge_response(SILVER_SYMBOL, '白银')


@app.route('/api/silver/spot_hist_sge', methods=['GET'])
def api_historical_silver_price():
    """历史白银价格 API 接口。"""
    return historical_sge_response(SILVER_SYMBOL, '白银')

@app.route('/api/gold/info', methods=['GET'])
def api_gold_info():
    """贵金属 API 信息接口。"""
    return jsonify({
        "name": "贵金属价格API服务",
        "version": API_VERSION,
        "description": "提供上金所 Au99.99 与 Ag99.99 的实时分时和交易日日线数据",
        "data_source": DATA_SOURCE,
        "market": get_sge_session_status(),
        "endpoints": {
            "/api/gold/spot_quotations_sge": "获取实时黄金价格",
            "/api/gold/spot_hist_sge": "获取历史黄金价格",
            "/api/gold/indicators/sma": "获取黄金日线收盘价 SMA 指标",
            "/api/gold/indicators/kdj": "获取黄金日线 KDJ 指标",
            "/api/gold/indicators/macd": "获取黄金日线 MACD（DIF、DEA）指标",
            "/api/gold/backtest": "运行黄金日线策略回测（MA、MACD、KDJ）",
            "/api/silver/spot_quotations_sge": "获取实时白银价格",
            "/api/silver/spot_hist_sge": "获取历史白银价格",
            "/api/market/session": "获取上金所常规交易时段与当前交易日归属",
            "/api/gold/info": "API信息"
        },
        "symbols": {
            "gold": {"symbol": GOLD_SYMBOL, "unit": GOLD_UNIT},
            "silver": {"symbol": SILVER_SYMBOL, "unit": SILVER_UNIT}
        }
    })


@app.route('/api/market/session', methods=['GET'])
def api_market_session():
    """Return the regular SGE session calendar and the current market state."""
    return jsonify({
        'status': 'success',
        'data_source': DATA_SOURCE,
        'market': get_sge_session_status(),
    })

# 推送服务 API
@app.route('/api/service/status', methods=['GET'])
@require_admin_token
def api_service_status():
    """获取服务状态"""
    return jsonify({
        'status': 'success',
        'scheduler_mode': 'external-process',
        'scheduler_status': '由进程管理器和调度进程日志提供',
        'message': '定时推送由独立调度进程管理，Web Worker 不提供其运行状态',
        'scheduled_pushes': {
            'day_opening': '工作日 09:02（取 09:00–09:05 首个有效报价）',
            'daily_settlement': '工作日 16:02（仅官方当日完整日线已发布时发送）',
            'night_opening': '工作日 20:02（归属下一交易日，取 20:00–20:05 首个有效报价）',
        },
        'market': get_sge_session_status(),
        'timestamp': market_timestamp()
    })

@app.route('/api/service/start', methods=['POST'])
@require_admin_token
def api_start_service():
    """In-process scheduler control is unsafe with multi-worker WSGI."""
    return scheduler_control_response()

@app.route('/api/service/stop', methods=['POST'])
@require_admin_token
def api_stop_service():
    """In-process scheduler control is unsafe with multi-worker WSGI."""
    return scheduler_control_response()

@app.route('/api/push/test', methods=['POST'])
@require_admin_token
def api_test_push():
    """测试推送"""
    try:
        result = gold_service.test_push()

        if result:
            return jsonify({
                'status': 'success',
                'message': '测试推送发送成功',
                'timestamp': market_timestamp()
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '测试推送发送失败',
                'timestamp': market_timestamp()
            }), 500

    except Exception as e:
        logger.error(f"测试推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'测试推送失败: {str(e)}',
            'timestamp': market_timestamp()
        }), 500

@app.route('/api/push/opening', methods=['POST'])
@require_admin_token
def api_push_opening():
    """推送开盘价"""
    try:
        success, message = gold_service.push_opening_price()
        return push_response(success, message)

    except Exception as e:
        logger.error(f"开盘价推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'开盘价推送失败: {str(e)}',
            'timestamp': market_timestamp()
        }), 500

@app.route('/api/push/closing', methods=['POST'])
@require_admin_token
def api_push_closing():
    """Send a confirmed close, or simulate the latest published daily close."""
    try:
        simulation = request.args.get('mode') == 'latest'
        success, message = gold_service.push_closing_price(simulation=simulation)
        return push_response(success, message)

    except Exception as e:
        logger.error(f"收盘价推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'收盘价推送失败: {str(e)}',
            'timestamp': market_timestamp()
        }), 500


@app.route('/api/push/night-opening', methods=['POST'])
@require_admin_token
def api_push_night_opening():
    """推送夜盘开盘快报。"""
    try:
        success, message = gold_service.push_night_opening_price()
        return push_response(success, message)
    except Exception as e:
        logger.error(f"夜盘开盘推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'夜盘开盘推送失败: {str(e)}',
            'timestamp': market_timestamp()
        }), 500

@app.route('/api/info', methods=['GET'])
def api_info():
    """服务信息"""
    return jsonify({
        'name': '贵金属价格服务',
        'version': API_VERSION,
        'description': '集成上金所实时分时、交易日日线与钉钉快报的服务',
        'features': {
            'gold_api': '上金所 Au99.99 实时分时与交易日日线',
            'silver_api': '上金所 Ag99.99 实时分时与交易日日线',
            'strategy_backtest': '后端日线策略回测与最新信号输出',
            'dingtalk_push': '日盘、日线收盘、夜盘三个时点的钉钉快报',
            'web_interface': 'Web管理界面'
        },
        'endpoints': {
            '/': 'Web管理界面',
            '/backtest': '后端策略回测页面',
            '/api/gold/*': '黄金价格API',
            '/api/silver/*': '白银价格API',
            '/api/market/session': '交易时段与交易日归属',
            '/api/service/status': '推送调度说明（需管理令牌）',
            '/api/push/*': '推送功能',
            '/api/info': '服务信息'
        },
        'schedule': {
            'day_opening': '工作日 09:02',
            'daily_settlement': '工作日 16:02',
            'night_opening': '工作日 20:02（归属下一交易日）'
        },
        'data_source': DATA_SOURCE,
        'market': get_sge_session_status(),
        'symbols': {
            'gold': GOLD_SYMBOL,
            'silver': SILVER_SYMBOL
        }
    })

def create_dingtalk_config():
    """创建钉钉配置文件"""
    config = {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN",
        "link_url": "http://gold.neoxmind.com/",
        "description": "钉钉机器人配置文件"
    }

    try:
        with open('dingtalk_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print("✅ 钉钉配置文件已创建: dingtalk_config.json")
        return True
    except Exception as e:
        print(f"❌ 创建配置文件失败: {e}")
        return False

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='黄金价格服务')
    parser.add_argument(
        '--scheduler-only',
        action='store_true',
        help='仅运行单实例定时推送调度器，不启动 Web 服务'
    )
    args = parser.parse_args()

    # 检查配置文件
    if not os.path.exists('dingtalk_config.json'):
        print("📝 首次运行，创建钉钉配置文件...")
        create_dingtalk_config()
        print("⚠️  请编辑 dingtalk_config.json 配置钉钉webhook地址")

    if args.scheduler_only:
        print("🔔 启动独立定时推送调度器...")
        run_scheduler_forever()
    else:
        print("🏅 启动黄金价格服务...")
        print("🌐 服务地址: http://127.0.0.1:5080")
        print("📋 管理界面: http://127.0.0.1:5080")
        print("📖 API文档: http://127.0.0.1:5080/api/info")
        print("🔔 定时推送请单独运行: python gold_service.py --scheduler-only")
        print("-" * 50)

        app.run(debug=False, host='127.0.0.1', port=5080)
