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
from datetime import datetime
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
SUPPORTED_SMA_WINDOWS = (5, 10, 20, 30)
DEFAULT_INDICATOR_DAYS = 60
MAX_INDICATOR_DAYS = 365
KDJ_RSV_WINDOW = 9
KDJ_SMOOTHING_PERIOD = 3
MACD_FAST_PERIOD = 12
MACD_SLOW_PERIOD = 26
MACD_SIGNAL_PERIOD = 9
SCHEDULED_PUSH_TIMES = {
    '09:00': ('opening', '开盘', lambda: gold_service.push_opening_price()),
    '16:00': ('closing', '收盘', lambda: gold_service.push_closing_price()),
}
SCHEDULER_CONTROL_MESSAGE = (
    '定时推送仅能由独立调度进程管理；请使用 '
    '`python gold_service.py --scheduler-only` 启动。'
)
scheduled_pushes = set()


def market_now():
    """Return the current time in the Shanghai gold-market timezone."""
    return datetime.now(MARKET_TIMEZONE)


def market_timestamp():
    """Return an unambiguous ISO-8601 timestamp with the UTC+08:00 offset."""
    return market_now().isoformat(timespec='milliseconds')


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

    def _get_cached_data(self, cache_key, loader, ttl_seconds, on_refresh=None):
        """Return fresh cached data and serialize cache misses per process."""
        now = time.monotonic()
        with self._cache_lock:
            cached = self._data_cache.get(cache_key)
            if (
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

    def get_historical_sge_price(self, symbol, days=30):
        """获取历史数据；交易时段缓存 30 分钟，其他时段缓存 12 小时。"""
        try:
            spot_hist_sge_df = self._get_cached_data(
                f'spot_hist_sge:{symbol}',
                lambda: ak.spot_hist_sge(symbol=symbol),
                historical_cache_ttl_seconds(),
                on_refresh=lambda: self._evict_cached_indicators(symbol),
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

    def get_historical_gold_price(self, days=30):
        """获取 Au99.99 历史价格。"""
        return self.get_historical_sge_price(GOLD_SYMBOL, days)

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
        """判断是否为交易日（排除周六周日）"""
        today = market_now().weekday()  # 0=Monday, 6=Sunday
        return today < 5  # Monday(0) to Friday(4)

    def _record_successful_push(self):
        """Record only confirmed deliveries in the local scheduler status."""
        service_status['last_push'] = market_timestamp()
        service_status['push_count'] += 1

    def push_opening_price(self):
        """推送开盘价格"""
        if not self.is_trading_day():
            logger.info("今日为周末，跳过开盘价格推送")
            return False, '今日非交易日，未发送开盘价格'

        logger.info("开始推送开盘价格...")

        # 获取实时数据,查找9:00的价格作为开盘价
        spot_data = self.get_real_time_gold_price()

        if spot_data is not None and not spot_data.empty:
            # 查找9:00:00的记录作为开盘价
            from datetime import time as dt_time
            nine_am = dt_time(9, 0, 0)

            # 筛选9点的数据
            nine_oclock_data = spot_data[spot_data['时间'] == nine_am]

            if not nine_oclock_data.empty:
                # 使用9点的现价作为开盘价
                open_price = nine_oclock_data.iloc[0]['现价']
                logger.info(f"找到9:00开盘价: {open_price}")
            else:
                # spot_quotations_sge 的跨零点过滤可能只留下夜盘残片。不能将
                # 最后一条（例如 02:29）静默伪装成 09:00 的开盘价。
                logger.error('未获取到 09:00 开盘价，拒绝发送开盘推送')
                return False, '未获取到 09:00 开盘价，未发送开盘价格'

            # 获取前一日收盘价
            hist_data_full = self.get_historical_gold_price()
            prev_close = 'N/A'

            if hist_data_full is not None and not hist_data_full.empty:
                # 获取最后一条历史记录
                last_hist = hist_data_full.iloc[-1]
                last_date = last_hist.get('date')

                # 检查是否是今天的数据
                from datetime import datetime as dt
                if isinstance(last_date, str):
                    record_date = dt.strptime(last_date, '%Y-%m-%d').date()
                else:
                    record_date = last_date.date() if hasattr(last_date, 'date') else last_date

                current_date = market_now().date()

                # 如果最后一条是今天的,前一日收盘是倒数第二条
                if record_date == current_date and len(hist_data_full) >= 2:
                    prev_close = hist_data_full.iloc[-2]['close']
                else:
                    # 否则最后一条就是前一日收盘
                    prev_close = last_hist['close']

            # 计算涨跌额和涨跌幅
            if isinstance(prev_close, (int, float)) and prev_close != 0:
                change = open_price - prev_close
                change_percent = (change / prev_close) * 100
            else:
                change = 0
                change_percent = 0

            # 涨跌表情
            trend_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"

            # 准备消息数据
            message_data = {
                'date': market_now().strftime('%Y-%m-%d'),
                'time': market_now().strftime('%H:%M:%S'),
                'symbol': 'Au99.99 (上海黄金交易所)',
                'open_price': open_price,
                'prev_close': prev_close if isinstance(prev_close, (int, float)) else 'N/A',
                'change': change,
                'change_percent': change_percent,
                'trend_emoji': trend_emoji,
                'link_url': self.link_url
            }

            message = MessageTemplate.format_opening_price_message(message_data)
            sent = self.send_message(message, title="黄金开盘价格播报")
            if sent:
                self._record_successful_push()
                return True, '开盘价格推送成功'

            logger.error('开盘价格推送未被钉钉接受')
            return False, '钉钉未接受开盘价格推送'
        else:
            error_data = {
                'date': market_now().strftime('%Y-%m-%d'),
                'time': market_now().strftime('%H:%M:%S'),
                'link_url': self.link_url
            }
            error_msg = MessageTemplate.format_error_message(error_data)
            self.send_message(error_msg, title="数据获取失败")
            return False, '无法获取开盘价格数据'

    def push_closing_price(self):
        """推送收盘价格"""
        if not self.is_trading_day():
            logger.info("今日为周末，跳过收盘价格推送")
            return False, '今日非交易日，未发送收盘价格'

        logger.info("开始推送收盘价格...")

        # 获取历史数据并检查日期
        hist_data_full = self.get_historical_gold_price()

        if hist_data_full is not None and not hist_data_full.empty:
            # 检查最后一条记录的日期
            last_record = hist_data_full.iloc[-1]
            last_date = last_record.get('date')

            # 转换日期进行比较
            from datetime import datetime as dt
            if isinstance(last_date, str):
                record_date = dt.strptime(last_date, '%Y-%m-%d').date()
            else:
                record_date = last_date.date() if hasattr(last_date, 'date') else last_date

            current_date = market_now().date()

            # 判断最后一条是否是今天的数据
            if record_date == current_date:
                # 官方日线已提供完整交易日（含夜盘）的 OHLC，不能用跨零点的
                # spot_quotations_sge 实时残片重新计算高低价。
                hist_data = last_record

                # 前一日数据是倒数第二条
                if len(hist_data_full) >= 2:
                    prev_data = hist_data_full.iloc[-2]
                    prev_close = prev_data.get('close', 0)
                else:
                    prev_close = 0
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
                'date': market_now().strftime('%Y-%m-%d'),
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
                'link_url': self.link_url
            }

            message = MessageTemplate.format_closing_price_message(message_data)
            sent = self.send_message(message, title="黄金收盘价格播报")
            if sent:
                self._record_successful_push()
                return True, '收盘价格推送成功'

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
    """首页 - Vue.js版本"""
    from flask import render_template
    return render_template('index.html')

def serialize_sge_records(data, historical=False):
    """Convert SGE data frames to JSON-safe records."""
    data_dict = data.to_dict('records')
    for record in data_dict:
        for key, value in record.items():
            if hasattr(value, 'strftime'):
                if historical:
                    record[key] = (
                        value.strftime('%Y-%m-%dT%H:%M:%S.000')
                        if hasattr(value, 'hour')
                        else value.strftime('%Y-%m-%dT00:00:00.000')
                    )
                else:
                    record[key] = (
                        value.strftime('%H:%M:%S')
                        if hasattr(value, 'hour')
                        else value.strftime('%Y-%m-%d')
                    )
    return data_dict


def clean_historical_close_data(data):
    """Return ordered, de-duplicated SGE daily dates and numeric close prices.

    Source data is intentionally cleaned before rolling calculations: trading
    dates are sorted ascending, malformed dates/prices are ignored, and a
    duplicate date keeps the last source record.
    """
    if data is None or data.empty:
        return pd.DataFrame(columns=['date', 'close'])

    if 'date' not in data.columns or 'close' not in data.columns:
        raise ValueError('历史行情缺少 date 或 close 字段')

    cleaned = data.loc[:, ['date', 'close']].copy()
    cleaned['date'] = pd.to_datetime(cleaned['date'], errors='coerce').dt.normalize()
    cleaned['close'] = pd.to_numeric(cleaned['close'], errors='coerce')
    cleaned = cleaned.dropna(subset=['date', 'close'])
    cleaned = cleaned[cleaned['close'].map(math.isfinite)]
    cleaned = cleaned.sort_values('date', kind='stable')
    cleaned = cleaned.drop_duplicates(subset=['date'], keep='last')
    return cleaned.reset_index(drop=True)


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


def serialize_sma_records(data, windows):
    """Convert a calculated SMA data frame into the public response shape."""
    records = []
    for _, row in data.iterrows():
        date = row['date']
        record = {
            'date': date.strftime('%Y-%m-%d'),
            'close': float(row['close']),
        }
        for window in windows:
            value = row[f'ma{window}']
            record[f'ma{window}'] = None if pd.isna(value) else float(value)
        records.append(record)
    return records


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
    """Return ordered valid daily high-low-close data for range indicators."""
    if data is None or data.empty:
        return pd.DataFrame(columns=['date', 'high', 'low', 'close'])

    required_columns = {'date', 'high', 'low', 'close'}
    missing_columns = required_columns.difference(data.columns)
    if missing_columns:
        raise ValueError(f"历史行情缺少 {'、'.join(sorted(missing_columns))} 字段")

    cleaned = data.loc[:, ['date', 'high', 'low', 'close']].copy()
    cleaned['date'] = pd.to_datetime(cleaned['date'], errors='coerce').dt.normalize()
    for column in ('high', 'low', 'close'):
        cleaned[column] = pd.to_numeric(cleaned[column], errors='coerce')
    cleaned = cleaned.dropna(subset=['date', 'high', 'low', 'close'])
    cleaned = cleaned[
        cleaned[['high', 'low', 'close']].map(math.isfinite).all(axis=1)
        & (cleaned['high'] >= cleaned['low'])
    ]
    cleaned = cleaned.sort_values('date', kind='stable')
    cleaned = cleaned.drop_duplicates(subset=['date'], keep='last')
    return cleaned.reset_index(drop=True)


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
    records = []
    for _, row in data.iterrows():
        record = {
            'date': row['date'].strftime('%Y-%m-%d'),
            'close': float(row['close']),
        }
        for field in ('k', 'd', 'j'):
            record[field] = None if pd.isna(row[field]) else float(row[field])
        records.append(record)
    return records


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
    records = []
    for _, row in data.iterrows():
        records.append({
            'date': row['date'].strftime('%Y-%m-%d'),
            'close': float(row['close']),
            'dif': float(row['dif']),
            'dea': float(row['dea']),
        })
    return records


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


def realtime_sge_response(symbol, metal_name):
    """Build a realtime SGE API response for a metal symbol."""
    try:
        data = gold_service.get_real_time_sge_price(symbol)
        if data is not None and not data.empty:
            result = {
                "status": "success",
                "timestamp": market_timestamp(),
                "data": serialize_sge_records(data),
                "count": len(data)
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
        days = request.args.get('days', type=int)
        data = gold_service.get_historical_sge_price(symbol, days)
        if data is not None and not data.empty:
            result = {
                "status": "success",
                "timestamp": market_timestamp(),
                "data": serialize_sge_records(data, historical=True),
                "count": len(data),
                "days": days or "all"
            }
            return jsonify(result)
        else:
            return jsonify({
                "status": "error",
                "message": f"无法获取历史{metal_name}价格数据",
                "timestamp": market_timestamp()
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": market_timestamp()
        }), 500


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
        "version": "2.0.0",
        "description": "提供上海黄金交易所 Au99.99 与 Ag99.99 的实时和历史价格数据",
        "endpoints": {
            "/api/gold/spot_quotations_sge": "获取实时黄金价格",
            "/api/gold/spot_hist_sge": "获取历史黄金价格",
            "/api/gold/indicators/sma": "获取黄金日线收盘价 SMA 指标",
            "/api/gold/indicators/kdj": "获取黄金日线 KDJ 指标",
            "/api/gold/indicators/macd": "获取黄金日线 MACD（DIF、DEA）指标",
            "/api/silver/spot_quotations_sge": "获取实时白银价格",
            "/api/silver/spot_hist_sge": "获取历史白银价格",
            "/api/gold/info": "API信息"
        },
        "data_source": "上海黄金交易所",
        "symbols": {
            "gold": GOLD_SYMBOL,
            "silver": SILVER_SYMBOL
        }
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
    """推送收盘价"""
    try:
        success, message = gold_service.push_closing_price()
        return push_response(success, message)

    except Exception as e:
        logger.error(f"收盘价推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'收盘价推送失败: {str(e)}',
            'timestamp': market_timestamp()
        }), 500

@app.route('/api/info', methods=['GET'])
def api_info():
    """服务信息"""
    return jsonify({
        'name': '贵金属价格服务',
        'version': '2.0.0',
        'description': '集成黄金价格API和钉钉推送功能的统一服务',
        'features': {
            'gold_api': '上海黄金交易所 Au99.99 价格数据',
            'silver_api': '上海黄金交易所 Ag99.99 价格数据',
            'dingtalk_push': '定时推送到钉钉群',
            'web_interface': 'Web管理界面'
        },
        'endpoints': {
            '/': 'Web管理界面',
            '/api/gold/*': '黄金价格API',
            '/api/silver/*': '白银价格API',
            '/api/service/*': '推送服务管理',
            '/api/push/*': '推送功能',
            '/api/info': '服务信息'
        },
        'schedule': {
            'opening': '工作日 09:00',
            'closing': '工作日 16:00'
        },
        'data_source': '上海黄金交易所',
        'symbols': {
            'gold': GOLD_SYMBOL,
            'silver': SILVER_SYMBOL
        }
    })

def create_dingtalk_config():
    """创建钉钉配置文件"""
    config = {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN",
        "link_url": "http://127.0.0.1:5080",
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
