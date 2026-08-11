import sys
import tempfile
import unittest
import os
from datetime import datetime, time
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


def strategy_history():
    closes = [100, 99, 98, 97, 96, 97, 98, 99, 100, 101, 102, 101, 100, 99, 98, 97,
              96, 95, 96, 97, 98, 99, 100, 101, 102, 103, 104, 103, 102, 101, 100, 99,
              98, 97, 96, 97, 98, 99, 100, 101]
    return pd.DataFrame({
        'date': pd.bdate_range('2026-01-01', periods=len(closes)),
        'high': [value + 2 for value in closes],
        'low': [value - 2 for value in closes],
        'close': closes,
    })


def resonance_indicator_frame():
    """Two aligned rows where every strategy has a 4x long resonance signal."""
    return pd.DataFrame({
        'date': pd.bdate_range('2026-06-01', periods=2),
        'high': [102, 112],
        'low': [98, 108],
        'close': [100, 110],
        'ma5': [100, 105],
        'ma10': [100, 104],
        'ma20': [100, 102],
        'ma30': [100, 102],
        'ma60': [100, 100],
        'dif': [0.1, 1.0],
        'dea': [0.2, 0.5],
        'k': [50, 60],
        'd': [50, 55],
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

    def test_realtime_quote_time_includes_shanghai_date_and_offset(self):
        quotes = pd.DataFrame({'时间': [time(14, 30, 5)], '现价': [892.25]})
        market_time = datetime(2026, 7, 27, 14, 31, tzinfo=gold_service.MARKET_TIMEZONE)

        records = gold_service.serialize_sge_records(quotes, market_time=market_time)

        self.assertEqual(records[0]['时间'], '2026-07-27T14:30:05+08:00')

    def test_market_session_labels_night_quotes_with_the_next_trading_day(self):
        friday_evening = datetime(2026, 7, 24, 20, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        saturday_early = datetime(2026, 7, 25, 1, 0, tzinfo=gold_service.MARKET_TIMEZONE)

        friday_status = gold_service.get_sge_session_status(friday_evening)
        saturday_status = gold_service.get_sge_session_status(saturday_early)

        self.assertEqual(friday_status['session'], 'night')
        self.assertEqual(friday_status['trading_day'], '2026-07-27')
        self.assertEqual(saturday_status['session'], 'night')
        self.assertEqual(saturday_status['trading_day'], '2026-07-27')

    def test_market_session_endpoint_exposes_timezone_and_daily_bar_semantics(self):
        response = self.client.get('/api/market/session')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['status'], 'success')
        self.assertEqual(body['market']['timezone'], 'Asia/Shanghai')
        self.assertIn('夜盘', body['market']['daily_bar_note'])

    def test_scheduled_pushes_include_afternoon_opening_and_1532_settlement(self):
        self.assertIn('13:32', gold_service.SCHEDULED_PUSH_TIMES)
        self.assertEqual(gold_service.SCHEDULED_PUSH_TIMES['13:32'][0], 'afternoon_opening')
        self.assertIn('15:32', gold_service.SCHEDULED_PUSH_TIMES)
        self.assertNotIn('16:32', gold_service.SCHEDULED_PUSH_TIMES)
        self.assertNotIn('16:02', gold_service.SCHEDULED_PUSH_TIMES)
        self.assertNotIn('20:02', gold_service.SCHEDULED_PUSH_TIMES)

    def test_opening_push_retries_until_a_quote_is_available_in_the_window(self):
        first_attempt = datetime(2026, 8, 11, 9, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        retry_attempt = datetime(2026, 8, 11, 9, 3, tzinfo=gold_service.MARKET_TIMEZONE)
        gold_service.scheduled_pushes.clear()

        with (
            patch.object(gold_service, 'market_now', side_effect=[first_attempt, retry_attempt]),
            patch.object(gold_service.gold_service, 'push_opening_price', side_effect=[
                (False, '未获取到日盘开盘报价，未发送推送'),
                (True, '日盘开盘价格推送成功'),
            ]) as push,
        ):
            gold_service.run_due_pushes()
            gold_service.run_due_pushes()

        self.assertEqual(push.call_count, 2)
        self.assertIn('2026-08-11:day_opening', gold_service.scheduled_pushes)

    def test_server_side_ma_backtest_executes_crosses_and_returns_latest_signal(self):
        result = gold_service.run_strategy_backtest(strategy_history(), strategy='ma5_20')

        self.assertEqual(result['strategy'], 'ma5_20')
        self.assertEqual(result['summary']['executed_trade_count'], 2)
        self.assertEqual(
            [(trade['action'], trade['execution_status']) for trade in result['trades']],
            [('buy', 'executed'), ('sell', 'executed')],
        )
        self.assertEqual(result['latest_signal']['action'], 'hold')
        self.assertEqual(result['assumptions']['execution_price'], '信号日收盘价')

    def test_backtest_marks_unaffordable_buy_as_skipped_instead_of_partially_buying(self):
        result = gold_service.run_strategy_backtest(
            strategy_history(),
            strategy='ma5_20',
            initial_cash=5_000,
            order_amount=10_000,
        )

        buy = next(trade for trade in result['trades'] if trade['action'] == 'buy')
        self.assertEqual(buy['execution_status'], 'skipped')
        self.assertEqual(buy['amount'], buy['base_order_amount'] * buy['signal_weight'])
        self.assertEqual(buy['quantity'], 0.0)
        self.assertEqual(buy['cash_after'], 5_000.0)
        self.assertIn('可用资金不足', buy['reason'])

    def test_all_backtest_strategies_use_other_indicators_as_resonance_weight(self):
        frame = resonance_indicator_frame()
        expected_confirmations = {
            'ma5_20': {'ma10_30', 'macd', 'kdj'},
            'ma10_30': {'ma5_20', 'macd', 'kdj'},
            'macd': {'ma5_20', 'ma10_30', 'kdj'},
            'kdj': {'ma5_20', 'ma10_30', 'macd'},
        }

        with patch.object(gold_service, 'build_backtest_indicators', return_value=frame):
            for strategy, confirmations in expected_confirmations.items():
                with self.subTest(strategy=strategy):
                    result = gold_service.run_strategy_backtest(
                        frame,
                        strategy=strategy,
                        initial_cash=100_000,
                        order_amount=10_000,
                    )

                    trade = result['trades'][0]
                    self.assertEqual(trade['execution_status'], 'executed')
                    self.assertEqual(trade['signal_weight'], 4)
                    self.assertEqual(trade['amount'], 40_000.0)
                    self.assertEqual(set(trade['confirmations']), confirmations)
                    self.assertEqual(result['latest_signal']['signal_weight'], 4)

    def test_macd_buy_requires_dif_and_dea_above_zero_axis(self):
        frame = resonance_indicator_frame()
        frame.loc[1, ['dif', 'dea']] = [0.1, -0.1]

        self.assertIsNone(gold_service._backtest_signal(frame, 1, 'macd'))

    def test_closing_strategy_signal_uses_kdj_as_the_only_primary_strategy(self):
        frame = resonance_indicator_frame()
        with patch.object(gold_service, 'build_backtest_indicators', return_value=frame):
            signals = gold_service.collect_closing_strategy_signals(frame, frame.iloc[-1]['date'])

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0]['strategy'], 'kdj')
        self.assertEqual(signals[0]['action'], 'buy')
        self.assertEqual(signals[0]['signal_weight'], 5)
        self.assertEqual(
            signals[0]['confirmations'],
            ['MA5/20', 'MA10/30', 'MACD', 'MACD 零轴'],
        )

    def test_closing_push_sends_strategy_action_and_weight_as_separate_alert(self):
        service = gold_service.gold_service
        now = datetime(2026, 6, 2, 16, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01', '2026-06-02'],
            'open': [100.0, 101.0],
            'high': [102.0, 103.0],
            'low': [99.0, 100.0],
            'close': [101.0, 102.0],
        })
        signals = [{
            'strategy_name': 'KDJ',
            'action': 'buy',
            'signal_weight': 5,
            'confirmations': ['MA5/20', 'MA10/30', 'MACD', 'MACD 零轴'],
        }]
        with (
            patch.object(service, 'is_trading_day', return_value=True),
            patch.object(service, 'get_historical_gold_price', return_value=history),
            patch.object(service, 'send_message', return_value=True) as sender,
            patch.object(gold_service, 'collect_closing_strategy_signals', return_value=signals),
            patch.object(gold_service, 'market_now', return_value=now),
        ):
            success, _ = service.push_closing_price()

        self.assertTrue(success)
        self.assertEqual(sender.call_count, 2)
        closing_message = sender.call_args_list[0].args[0]
        strategy_message = sender.call_args_list[1].args[0]
        self.assertNotIn('KDJ 买入', closing_message)
        self.assertIn('🚨 黄金策略交易信号', strategy_message)
        self.assertIn('KDJ 买入', strategy_message)
        self.assertIn('买入权重 x5', strategy_message)
        self.assertIn('MA5/20、MA10/30、MACD、MACD 零轴', strategy_message)
        self.assertEqual(sender.call_args_list[1].kwargs['title'], '🚨 黄金策略信号')

    def test_closing_push_aggregates_realtime_ohlc_when_official_daily_bar_is_delayed(self):
        service = gold_service.gold_service
        now = datetime(2026, 6, 2, 15, 32, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01'],
            'open': [100.0],
            'high': [102.0],
            'low': [99.0],
            'close': [101.0],
        })
        quotes = pd.DataFrame({
            '时间': [time(9), time(10), time(14), time(15, 30)],
            '现价': [102.0, 101.0, 105.0, 104.0],
        })
        with (
            patch.object(service, 'is_trading_day', return_value=True),
            patch.object(service, 'get_historical_gold_price', return_value=history),
            patch.object(service, 'get_real_time_gold_price', return_value=quotes),
            patch.object(service, 'send_message', return_value=True) as sender,
            patch.object(gold_service, 'market_now', return_value=now),
        ):
            success, message = service.push_closing_price()

        self.assertTrue(success)
        self.assertEqual(message, '收盘价格推送成功')
        sender.assert_called_once()
        closing_message = sender.call_args.args[0]
        self.assertIn('开盘价:** 102.0', closing_message)
        self.assertIn('最低-高价:** 101.0 ~ 105.0', closing_message)
        self.assertIn('收盘价:** 104.0', closing_message)
        self.assertIn('官方日线尚未发布，本快报按当日实时分时行情聚合', closing_message)

    def test_manual_kdj_signal_push_sends_only_the_triggered_strategy_message(self):
        service = gold_service.gold_service
        market_time = datetime(2026, 6, 2, 16, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01', '2026-06-02'],
            'open': [100.0, 101.0],
            'high': [102.0, 103.0],
            'low': [99.0, 100.0],
            'close': [101.0, 102.0],
        })
        signal = {
            'strategy_name': 'KDJ',
            'action': 'buy',
            'signal_weight': 5,
            'confirmations': ['MA5/20', 'MA10/30', 'MACD', 'MACD 零轴'],
        }
        with (
            patch.object(service, 'get_historical_gold_price', return_value=history) as loader,
            patch.object(service, 'send_message', return_value=True) as sender,
            patch.object(gold_service, 'collect_closing_strategy_signals', return_value=[signal]),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            success, message = service.push_latest_kdj_strategy_signal()

        self.assertTrue(success)
        self.assertIn('2026-06-02', message)
        loader.assert_called_once_with(force_refresh=True)
        self.assertEqual(sender.call_count, 1)
        self.assertIn('KDJ 买入', sender.call_args.args[0])
        self.assertEqual(sender.call_args.kwargs['title'], '🚨 黄金策略信号（模拟）')

    def test_manual_kdj_signal_push_reports_when_latest_bar_has_no_cross(self):
        service = gold_service.gold_service
        market_time = datetime(2026, 6, 2, 16, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01', '2026-06-02'],
            'close': [101.0, 102.0],
        })
        with (
            patch.object(service, 'get_historical_gold_price', return_value=history),
            patch.object(service, 'send_message') as sender,
            patch.object(gold_service, 'collect_closing_strategy_signals', return_value=[]),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            success, message = service.push_latest_kdj_strategy_signal()

        self.assertIsNone(success)
        self.assertIn('未触发 KDJ', message)
        sender.assert_not_called()

    def test_manual_kdj_signal_push_uses_a_realtime_bar_before_daily_history_is_published(self):
        service = gold_service.gold_service
        market_time = datetime(2026, 6, 2, 14, 30, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01'],
            'open': [100.0],
            'high': [102.0],
            'low': [99.0],
            'close': [101.0],
        })
        quotes = pd.DataFrame({
            '时间': [time(9), time(10), time(14, 30)],
            '现价': [102.0, 104.0, 103.0],
        })
        signal = {
            'strategy_name': 'KDJ',
            'action': 'buy',
            'signal_weight': 5,
            'confirmations': ['MA5/20', 'MA10/30', 'MACD', 'MACD 零轴'],
        }
        collected_history = []
        def collect_signal(frame, trading_date):
            collected_history.append((frame, trading_date))
            return [signal]

        with (
            patch.object(service, 'get_historical_gold_price', return_value=history),
            patch.object(service, 'get_real_time_gold_price', return_value=quotes),
            patch.object(service, 'send_message', return_value=True) as sender,
            patch.object(gold_service, 'collect_closing_strategy_signals', side_effect=collect_signal),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            success, message = service.push_latest_kdj_strategy_signal()

        self.assertTrue(success)
        self.assertIn('当前盘中合成日线', message)
        frame, trading_date = collected_history[0]
        self.assertEqual(trading_date.isoformat(), '2026-06-02')
        self.assertEqual(frame.iloc[-1][['open', 'high', 'low', 'close']].to_dict(), {
            'open': 102.0,
            'high': 104.0,
            'low': 102.0,
            'close': 103.0,
        })
        self.assertIn('当前实时行情合成的盘中日线', sender.call_args.args[0])

    def test_simulated_closing_push_uses_latest_published_daily_bar(self):
        service = gold_service.gold_service
        now = datetime(2026, 6, 3, 16, 2, tzinfo=gold_service.MARKET_TIMEZONE)
        history = pd.DataFrame({
            'date': ['2026-06-01', '2026-06-02'],
            'open': [100.0, 101.0],
            'high': [102.0, 103.0],
            'low': [99.0, 100.0],
            'close': [101.0, 102.0],
        })
        with (
            patch.object(service, 'get_historical_gold_price', return_value=history),
            patch.object(service, 'send_message', return_value=True) as sender,
            patch.object(gold_service, 'collect_closing_strategy_signals', return_value=[]),
            patch.object(gold_service, 'market_now', return_value=now),
        ):
            success, message = service.push_closing_price(simulation=True)

        self.assertTrue(success)
        self.assertEqual(message, '收盘价格模拟推送成功')
        self.assertEqual(sender.call_args.kwargs['title'], '黄金收盘价格模拟播报')
        self.assertIn('2026-06-02', sender.call_args.args[0])
        self.assertIn('手动模拟推送', sender.call_args.args[0])

    def test_backtest_api_uses_full_history_and_exposes_dingtalk_reusable_latest_signal(self):
        history = strategy_history()
        with patch.object(gold_service.gold_service, 'get_historical_sge_price', return_value=history) as loader:
            response = self.client.get('/api/gold/backtest?strategy=kdj&start=2026-01-20&initial_cash=20000&order_amount=5000')

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(loader.call_args.args, (gold_service.GOLD_SYMBOL,))
        self.assertEqual(loader.call_args.kwargs, {'days': None})
        self.assertEqual(body['result']['strategy'], 'kdj')
        self.assertEqual(body['result']['summary']['initial_cash'], 20000.0)
        self.assertIn('action', body['result']['latest_signal'])
        self.assertIn('reason', body['result']['latest_signal'])
        self.assertIn('signal_weight', body['result']['latest_signal'])
        self.assertIn('confirmations', body['result']['latest_signal'])

    def test_backtest_api_rejects_invalid_strategy_dates_and_money(self):
        for query in (
            'strategy=unknown',
            'start=2026-03-01&end=2026-02-01',
            'start=bad-date',
            'initial_cash=0',
            'order_amount=bad',
            'initial_cash=10000&order_amount=10001',
            'initial_cash=100001',
            'order_amount=10001',
        ):
            with self.subTest(query=query):
                response = self.client.get(f'/api/gold/backtest?{query}')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['status'], 'error')

    def test_backtest_defaults_to_2025_start_current_date_and_standard_cash(self):
        current = datetime(2026, 7, 29, 14, 0, tzinfo=gold_service.MARKET_TIMEZONE)
        with (
            self.client.application.test_request_context('/api/gold/backtest'),
            patch.object(gold_service, 'market_now', return_value=current),
        ):
            options = gold_service.parse_backtest_parameters()

        self.assertEqual(options['start_date'].isoformat(), '2025-01-01')
        self.assertEqual(options['end_date'].isoformat(), '2026-07-29')
        self.assertEqual(options['initial_cash'], 100000.0)
        self.assertEqual(options['order_amount'], 10000.0)

    def test_backtest_page_is_served_independently_from_the_main_frontend(self):
        response = self.client.get('/backtest')

        self.assertEqual(response.status_code, 200)
        self.assertIn('后端策略回测', response.get_data(as_text=True))
        self.assertIn('static/js/backtest.js', response.get_data(as_text=True))
        self.assertIn('value="2025-01-01"', response.get_data(as_text=True))

    def test_realtime_quotes_from_a_stale_weekend_feed_keep_their_trading_day(self):
        quotes = pd.DataFrame({'时间': [time(9), time(11), time(1)], '现价': [890, 891, 892]})
        saturday_morning = datetime(2026, 7, 25, 10, 0, tzinfo=gold_service.MARKET_TIMEZONE)

        records = gold_service.serialize_sge_records(quotes, market_time=saturday_morning)

        self.assertEqual(records[0]['时间'], '2026-07-24T09:00:00+08:00')
        self.assertEqual(records[1]['时间'], '2026-07-24T11:00:00+08:00')
        self.assertEqual(records[2]['时间'], '2026-07-25T01:00:00+08:00')

    def test_current_tick_can_be_a_few_minutes_ahead_of_the_server_clock(self):
        quotes = pd.DataFrame({'时间': [time(9, 0, 2)], '现价': [892.25]})
        market_time = datetime(2026, 7, 27, 9, 0, tzinfo=gold_service.MARKET_TIMEZONE)

        records = gold_service.serialize_sge_records(quotes, market_time=market_time)

        self.assertEqual(records[0]['时间'], '2026-07-27T09:00:02+08:00')

    def test_close_tick_is_serialized_and_does_not_break_realtime_api(self):
        quotes = pd.DataFrame({'时间': [time(15, 29), time(15, 30)], '现价': [892.25, 892.30]})
        market_time = datetime(2026, 7, 27, 15, 31, tzinfo=gold_service.MARKET_TIMEZONE)

        with (
            patch.object(gold_service.gold_service, 'get_real_time_sge_price', return_value=quotes),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            response = self.client.get('/api/gold/spot_quotations_sge?symbol=Au99.99')

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item['时间'] for item in response.get_json()['data']],
            ['2026-07-27T15:29:00+08:00', '2026-07-27T15:30:00+08:00']
        )

    def test_count_matches_the_rows_actually_returned(self):
        # 12:00 不属于任何交易时段，该行会被序列化器丢弃；
        # count 必须描述实际返回的条数，而不是上游原始长度。
        quotes = pd.DataFrame({'时间': [time(10, 0), time(12, 0)], '现价': [892.25, 892.30]})
        market_time = datetime(2026, 7, 27, 15, 31, tzinfo=gold_service.MARKET_TIMEZONE)

        with (
            patch.object(gold_service.gold_service, 'get_real_time_sge_price', return_value=quotes),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            response = self.client.get('/api/gold/spot_quotations_sge?symbol=Au99.99')

        body = response.get_json()
        self.assertEqual(len(body['data']), 1)
        self.assertEqual(body['count'], 1)

    def test_realtime_response_includes_symbol_unit_and_market_metadata(self):
        quotes = pd.DataFrame({'时间': [time(9, 0)], '现价': [892.25]})
        market_time = datetime(2026, 7, 27, 9, 1, tzinfo=gold_service.MARKET_TIMEZONE)

        with (
            patch.object(gold_service.gold_service, 'get_real_time_sge_price', return_value=quotes),
            patch.object(gold_service, 'market_now', return_value=market_time),
        ):
            response = self.client.get('/api/gold/spot_quotations_sge')

        body = response.get_json()
        self.assertEqual(body['symbol'], gold_service.GOLD_SYMBOL)
        self.assertEqual(body['unit'], gold_service.GOLD_UNIT)
        self.assertEqual(body['market']['trading_day'], '2026-07-27')

    def test_duplicate_history_date_keeps_the_last_record(self):
        # 同一交易日出现多条时以最后一条为准；两个清洗入口必须一致，
        # 否则同一份历史在 SMA 和 KDJ/MACD 上会得出不同的收盘价。
        duplicated = pd.DataFrame({
            'date': ['2026-07-24', '2026-07-24', '2026-07-27'],
            'high': [12, 15, 16],
            'low': [8, 9, 10],
            'close': [10, 11, 12],
        })

        close_only = gold_service.clean_historical_close_data(duplicated)
        ohlc = gold_service.clean_historical_ohlc_data(duplicated)

        self.assertEqual(list(close_only['close']), [11, 12])
        self.assertEqual(list(ohlc['close']), [11, 12])
        self.assertEqual(list(ohlc['high']), [15, 16])

    def test_ohlc_cleaner_drops_rows_whose_high_is_below_its_low(self):
        malformed = pd.DataFrame({
            'date': ['2026-07-24', '2026-07-27'],
            'high': [5, 16],
            'low': [9, 10],
            'close': [7, 12],
        })

        cleaned = gold_service.clean_historical_ohlc_data(malformed)

        self.assertEqual(list(cleaned['close']), [12])

    def test_serializers_emit_null_for_missing_rolling_values(self):
        # 滚动窗口前几行本就没有值，必须序列化为 null 而不是 NaN
        # （NaN 不是合法 JSON，会让整条响应无法解析）。
        frame = pd.DataFrame({
            'date': [pd.Timestamp('2026-07-27')],
            'close': [10.0],
            'ma5': [float('nan')],
            'k': [float('nan')],
            'd': [50.0],
            'j': [float('nan')],
        })

        sma = gold_service.serialize_sma_records(frame, [5])
        kdj = gold_service.serialize_kdj_records(frame)

        self.assertEqual(sma[0], {'date': '2026-07-27', 'close': 10.0, 'ma5': None})
        self.assertEqual(
            kdj[0],
            {'date': '2026-07-27', 'close': 10.0, 'k': None, 'd': 50.0, 'j': None},
        )

    def test_macd_serializer_keeps_its_fields_required(self):
        # MACD 的 dif/dea 没有暖机期，NaN 意味着计算坏了，
        # 不应被静默转成 null 掩盖过去。
        frame = pd.DataFrame({
            'date': [pd.Timestamp('2026-07-27')],
            'close': [10.0],
            'dif': [1.5],
            'dea': [1.25],
        })

        self.assertEqual(
            gold_service.serialize_macd_records(frame)[0],
            {'date': '2026-07-27', 'close': 10.0, 'dif': 1.5, 'dea': 1.25},
        )

    def test_history_date_is_interpreted_the_same_way_on_both_push_paths(self):
        # 开盘与收盘推送曾各自逐字重复这段归一化；现在共用一个函数。
        self.assertEqual(
            gold_service.normalize_history_date('2026-07-27'),
            gold_service.normalize_history_date(pd.Timestamp('2026-07-27')),
        )
        self.assertEqual(
            gold_service.normalize_history_date(datetime(2026, 7, 27, 15, 30)).isoformat(),
            '2026-07-27',
        )

    def test_calculates_all_requested_windows_from_complete_history(self):
        response, loader = self.request_with_history(
            daily_history(list(range(1, 81))),
            'days=5&windows=5,10,20,30,60',
        )

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(loader.call_args.args, (gold_service.GOLD_SYMBOL,))
        self.assertEqual(loader.call_args.kwargs, {'days': None})
        self.assertEqual(body['count'], 5)
        self.assertEqual(body['available_history_count'], 80)
        self.assertEqual(body['data'][0]['date'], '2026-09-14')
        self.assertEqual(body['data'][0]['ma30'], 61.5)
        self.assertEqual(body['data'][0]['ma60'], 46.5)
        self.assertEqual(body['latest'], body['data'][0 + 4])
        self.assertEqual(body['latest']['close'], 80.0)
        self.assertEqual(body['latest']['ma5'], 78.0)
        self.assertEqual(body['latest']['ma10'], 75.5)
        self.assertEqual(body['latest']['ma20'], 70.5)
        self.assertEqual(body['latest']['ma30'], 65.5)
        self.assertEqual(body['latest']['ma60'], 50.5)

    def test_default_windows_include_ma60(self):
        response, _ = self.request_with_history(daily_history(list(range(1, 81))))

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body['windows'], [5, 10, 20, 30, 60])
        self.assertEqual(body['latest']['ma60'], 50.5)

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

    def test_settlement_can_force_a_history_refresh_past_the_regular_cache(self):
        service = gold_service.GoldService()
        first = daily_history([1, 2])
        refreshed = daily_history([1, 2, 3])
        with patch.object(gold_service.ak, 'spot_hist_sge', side_effect=[first, refreshed]) as source:
            service.get_historical_sge_price(gold_service.GOLD_SYMBOL, days=None)
            data = service.get_historical_sge_price(
                gold_service.GOLD_SYMBOL,
                days=None,
                force_refresh=True,
            )

        self.assertEqual(source.call_count, 2)
        self.assertEqual(data['close'].tolist(), [1, 2, 3])

    def test_history_api_rejects_non_positive_or_non_numeric_days(self):
        for query in ('days=0', 'days=-3', 'days=bad'):
            with self.subTest(query=query):
                response = self.client.get(f'/api/gold/spot_hist_sge?{query}')
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.get_json()['status'], 'error')

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

    def test_drops_terminal_quote_at_the_1_36_percent_threshold(self):
        quotes = gold_service.drop_terminal_realtime_outlier(
            realtime_quotes([100.0, 98.64]),
            gold_service.GOLD_SYMBOL,
        )

        self.assertEqual(quotes['现价'].tolist(), [100.0])

    def test_keeps_normal_terminal_realtime_quote(self):
        quotes = gold_service.drop_terminal_realtime_outlier(
            realtime_quotes([770.0, 771.2, 771.5]),
            gold_service.GOLD_SYMBOL,
        )

        self.assertEqual(quotes['现价'].tolist(), [770.0, 771.2, 771.5])

    def test_cors_allows_only_explicitly_configured_origins(self):
        allowed_origin = 'https://frontend.example.com'
        with patch.dict(os.environ, {
            gold_service.CORS_ALLOWED_ORIGINS_ENV: allowed_origin,
        }):
            allowed = self.client.get('/api/info', headers={'Origin': allowed_origin})
            rejected = self.client.get('/api/info', headers={'Origin': 'https://untrusted.example.com'})
            preflight = self.client.options('/api/push/test', headers={
                'Origin': allowed_origin,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'X-Admin-Token',
            })

        self.assertEqual(allowed.headers.get('Access-Control-Allow-Origin'), allowed_origin)
        self.assertIn('Origin', allowed.headers.get('Vary', ''))
        self.assertIsNone(rejected.headers.get('Access-Control-Allow-Origin'))
        self.assertEqual(preflight.status_code, 200)
        self.assertEqual(preflight.headers.get('Access-Control-Allow-Headers'), 'Content-Type, X-Admin-Token')
        self.assertEqual(preflight.headers.get('Access-Control-Allow-Methods'), 'GET, POST, PUT, OPTIONS')

    def test_feishu_push_test_endpoint_targets_only_feishu(self):
        configured_service = object()
        with (
            patch.dict(os.environ, {gold_service.ADMIN_TOKEN_ENV: 'test-admin-token'}),
            patch.object(gold_service.gold_service.push_manager, 'get_service', return_value=configured_service),
            patch.object(gold_service.gold_service, 'test_push', return_value=True) as test_push,
        ):
            response = self.client.post(
                '/api/push/test/feishu',
                headers={'X-Admin-Token': 'test-admin-token'},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['message'], '飞书测试推送发送成功')
        test_push.assert_called_once_with(service_name='feishu')

    def test_feishu_push_test_endpoint_requires_enabled_configuration(self):
        with (
            patch.dict(os.environ, {gold_service.ADMIN_TOKEN_ENV: 'test-admin-token'}),
            patch.object(gold_service.gold_service.push_manager, 'get_service', return_value=None),
        ):
            response = self.client.post(
                '/api/push/test/feishu',
                headers={'X-Admin-Token': 'test-admin-token'},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()['message'], '飞书推送未配置或已禁用')

    def test_admin_can_save_channel_settings_without_reading_back_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'push_channels.json'
            with patch.dict(os.environ, {
                gold_service.ADMIN_TOKEN_ENV: 'test-admin-token',
                'GOLD_PUSH_CONFIG_PATH': str(config_path),
            }):
                saved = self.client.put(
                    '/api/push/config',
                    json={
                        'feishu': {
                            'enabled': True,
                            'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/test',
                            'secret': 'do-not-return-this',
                            'link_url': 'https://gold.example.com',
                        },
                    },
                    headers={'X-Admin-Token': 'test-admin-token'},
                )
                fetched = self.client.get(
                    '/api/push/config',
                    headers={'X-Admin-Token': 'test-admin-token'},
                )

        self.assertEqual(saved.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        channel = fetched.get_json()['channels']['feishu']
        self.assertTrue(channel['enabled'])
        self.assertTrue(channel['webhook_configured'])
        self.assertTrue(channel['secret_configured'])
        self.assertNotIn('webhook_url', channel)
        self.assertNotIn('secret', channel)

if __name__ == '__main__':
    unittest.main()
