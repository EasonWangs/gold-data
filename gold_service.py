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
import logging
import argparse
import hmac
import os
from functools import wraps
from flask import Flask, jsonify, request
from datetime import datetime
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
SCHEDULER_CONTROL_MESSAGE = (
    '定时推送仅能由独立调度进程管理；请使用 '
    '`python gold_service.py --scheduler-only` 启动。'
)


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
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 503

        if not hmac.compare_digest(provided_token, expected_token):
            return jsonify({
                'status': 'error',
                'message': '管理令牌无效',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 401

        return view(*args, **kwargs)

    return wrapped

class GoldService:
    def __init__(self):
        config = self.load_dingtalk_config()
        self.webhook_url = config.get('webhook_url') if config else None
        self.link_url = config.get('link_url', 'http://127.0.0.1:5080') if config else 'http://127.0.0.1:5080'
        self.push_manager = push_manager

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

    def get_real_time_gold_price(self):
        """获取实时黄金价格"""
        try:
            spot_quotations_sge_df = ak.spot_quotations_sge(symbol="Au99.99")
            return spot_quotations_sge_df
        except Exception as e:
            logger.error(f"获取实时黄金价格失败: {e}")
            return None

    def get_historical_gold_price(self, days=30):
        """获取历史黄金价格"""
        try:
            spot_hist_sge_df = ak.spot_hist_sge(symbol='Au99.99')
            if days and days > 0:
                return spot_hist_sge_df.tail(days)
            return spot_hist_sge_df
        except Exception as e:
            logger.error(f"获取历史黄金价格失败: {e}")
            return None

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
        today = datetime.now().weekday()  # 0=Monday, 6=Sunday
        return today < 5  # Monday(0) to Friday(4)

    def _record_successful_push(self):
        """Record only confirmed deliveries in the local scheduler status."""
        service_status['last_push'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
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

                current_date = datetime.now().date()

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
                'date': datetime.now().strftime('%Y-%m-%d'),
                'time': datetime.now().strftime('%H:%M:%S'),
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
                'date': datetime.now().strftime('%Y-%m-%d'),
                'time': datetime.now().strftime('%H:%M:%S'),
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

            current_date = datetime.now().date()

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
                    'date': datetime.now().strftime('%Y-%m-%d'),
                    'time': datetime.now().strftime('%H:%M:%S'),
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
                'date': datetime.now().strftime('%Y-%m-%d'),
                'time': datetime.now().strftime('%H:%M:%S'),
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
                'date': datetime.now().strftime('%Y-%m-%d'),
                'time': datetime.now().strftime('%H:%M:%S'),
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

def run_scheduler_forever():
    """Run the scheduler in this dedicated process only."""
    global scheduler_running
    if scheduler_running:
        raise RuntimeError('调度器已在此进程中运行')

    schedule.clear('gold-price')
    schedule.every().day.at("09:00").do(gold_service.push_opening_price).tag('gold-price')
    schedule.every().day.at("16:00").do(gold_service.push_closing_price).tag('gold-price')
    scheduler_running = True
    service_status['running'] = True
    service_status['start_time'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
    logger.info("📅 定时推送调度器已启动")

    try:
        while scheduler_running:
            try:
                schedule.run_pending()
            except Exception as e:
                logger.exception(f"调度器运行异常: {e}")
                service_status['errors'].append({
                    'time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000'),
                    'error': str(e)
                })
                # Keep error history bounded and prevent a failed due job from busy-looping.
                del service_status['errors'][:-100]
            time.sleep(60)  # 每分钟检查一次
    finally:
        scheduler_running = False
        service_status['running'] = False
        schedule.clear('gold-price')


def scheduler_control_response():
    """Reject in-process scheduler control from WSGI workers."""
    return jsonify({
        'status': 'error',
        'message': SCHEDULER_CONTROL_MESSAGE,
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
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
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
    }), status_code


@app.route('/')
def index():
    """首页 - Vue.js版本"""
    from flask import render_template
    return render_template('index.html')

# 黄金价格 API
@app.route('/api/gold/spot_quotations_sge', methods=['GET'])
def api_realtime_gold_price():
    """实时黄金价格API接口"""
    try:
        data = gold_service.get_real_time_gold_price()
        if data is not None and not data.empty:
            # 转换为字典格式，处理时间字段
            data_dict = data.to_dict('records')
            # 处理时间字段序列化问题
            for record in data_dict:
                for key, value in record.items():
                    if hasattr(value, 'strftime'):  # 检查是否为时间类型
                        record[key] = value.strftime('%H:%M:%S') if hasattr(value, 'hour') else value.strftime('%Y-%m-%d')

            result = {
                "status": "success",
                "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000'),
                "data": data_dict,
                "count": len(data)
            }
            return jsonify(result)
        else:
            return jsonify({
                "status": "error",
                "message": "无法获取实时黄金价格数据",
                "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/gold/spot_hist_sge', methods=['GET'])
def api_historical_gold_price():
    """历史黄金价格API接口"""
    try:
        # 获取查询参数
        days = request.args.get('days', type=int)

        data = gold_service.get_historical_gold_price(days)
        if data is not None and not data.empty:
            # 转换为字典格式，处理时间字段
            data_dict = data.to_dict('records')
            # 处理时间字段序列化问题，格式化为指定格式
            for record in data_dict:
                for key, value in record.items():
                    if hasattr(value, 'strftime'):  # 检查是否为时间类型
                        if hasattr(value, 'hour'):  # 如果包含时间部分
                            record[key] = value.strftime('%Y-%m-%dT%H:%M:%S.000')
                        else:  # 如果只是日期
                            record[key] = value.strftime('%Y-%m-%dT00:00:00.000')

            result = {
                "status": "success",
                "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000'),
                "data": data_dict,
                "count": len(data),
                "days": days or "all"
            }
            return jsonify(result)
        else:
            return jsonify({
                "status": "error",
                "message": "无法获取历史黄金价格数据",
                "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 500
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e),
            "timestamp": datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/gold/info', methods=['GET'])
def api_gold_info():
    """黄金API信息接口"""
    return jsonify({
        "name": "黄金价格API服务",
        "version": "2.0.0",
        "description": "提供上海黄金交易所Au99.99实时和历史价格数据",
        "endpoints": {
            "/api/gold/spot_quotations_sge": "获取实时黄金价格",
            "/api/gold/spot_hist_sge": "获取历史黄金价格",
            "/api/gold/info": "API信息"
        },
        "data_source": "上海黄金交易所",
        "symbol": "Au99.99"
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
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
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
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '测试推送发送失败',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 500

    except Exception as e:
        logger.error(f"测试推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'测试推送失败: {str(e)}',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
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
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
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
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/info', methods=['GET'])
def api_info():
    """服务信息"""
    return jsonify({
        'name': '黄金价格服务',
        'version': '2.0.0',
        'description': '集成黄金价格API和钉钉推送功能的统一服务',
        'features': {
            'gold_api': '上海黄金交易所Au99.99价格数据',
            'dingtalk_push': '定时推送到钉钉群',
            'web_interface': 'Web管理界面'
        },
        'endpoints': {
            '/': 'Web管理界面',
            '/api/gold/*': '黄金价格API',
            '/api/service/*': '推送服务管理',
            '/api/push/*': '推送功能',
            '/api/info': '服务信息'
        },
        'schedule': {
            'opening': '工作日 09:00',
            'closing': '工作日 16:00'
        },
        'data_source': '上海黄金交易所',
        'symbol': 'Au99.99'
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
