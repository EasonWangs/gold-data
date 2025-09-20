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
from flask import Flask, jsonify, request
from datetime import datetime

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 配置 Jinja2 以避免与 Vue.js 语法冲突
app.jinja_env.variable_start_string = '[['
app.jinja_env.variable_end_string = ']]'

# 全局变量
scheduler_thread = None
scheduler_running = False
service_status = {
    'running': False,
    'start_time': None,
    'last_push': None,
    'push_count': 0,
    'errors': []
}

class GoldService:
    def __init__(self):
        self.webhook_url = self.load_dingtalk_config()

    def load_dingtalk_config(self):
        """加载钉钉配置"""
        try:
            with open('dingtalk_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config.get('webhook_url')
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

    def send_dingtalk_message(self, message):
        """发送钉钉消息"""
        if not self.webhook_url:
            logger.error("webhook地址未配置，无法发送消息")
            return False

        headers = {'Content-Type': 'application/json'}
        data = {
            "msgtype": "text",
            "text": {
                "content": message
            }
        }

        try:
            response = requests.post(self.webhook_url, headers=headers, data=json.dumps(data))
            if response.status_code == 200:
                result = response.json()
                if result.get('errcode') == 0:
                    logger.info("钉钉消息发送成功")
                    return True
                else:
                    logger.error(f"钉钉消息发送失败: {result.get('errmsg')}")
                    return False
            else:
                logger.error(f"钉钉消息发送失败: HTTP {response.status_code}")
                return False
        except Exception as e:
            logger.error(f"发送钉钉消息异常: {e}")
            return False

    def is_trading_day(self):
        """判断是否为交易日（排除周六周日）"""
        today = datetime.now().weekday()  # 0=Monday, 6=Sunday
        return today < 5  # Monday(0) to Friday(4)

    def push_opening_price(self):
        """推送开盘价格"""
        if not self.is_trading_day():
            logger.info("今日为周末，跳过开盘价格推送")
            return

        logger.info("开始推送开盘价格...")
        data = self.get_gold_price_data()

        if data and data['history'] is not None:
            today = datetime.now().strftime('%Y-%m-%d')
            hist_data = data['history']

            message = f"""📈 黄金开盘价格播报 📈

📅 日期: {today}
🏅 品种: Au99.99 (上海黄金交易所)
💰 开盘价: {hist_data.get('open', 'N/A')} 元/克

⏰ 推送时间: {datetime.now().strftime('%H:%M:%S')}
📊 数据来源: 上海黄金交易所"""

            self.send_dingtalk_message(message)
            global service_status
            service_status['last_push'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            service_status['push_count'] += 1
        else:
            error_msg = f"""❌ 黄金价格数据获取失败

📅 日期: {datetime.now().strftime('%Y-%m-%d')}
⏰ 时间: {datetime.now().strftime('%H:%M:%S')}
🔧 请检查数据源连接"""
            self.send_dingtalk_message(error_msg)

    def push_closing_price(self):
        """推送收盘价格"""
        if not self.is_trading_day():
            logger.info("今日为周末，跳过收盘价格推送")
            return

        logger.info("开始推送收盘价格...")
        data = self.get_gold_price_data()

        if data and data['history'] is not None:
            today = datetime.now().strftime('%Y-%m-%d')
            hist_data = data['history']

            # 计算涨跌（相对于前一日收盘价）
            close_price = hist_data.get('close', 0) if hist_data is not None else 0
            prev_data = data['prev_history']
            prev_close = prev_data.get('close', 0) if prev_data is not None else 0

            # 涨跌额 = 今日收盘价 - 前一日收盘价
            change = close_price - prev_close if prev_close != 0 and close_price != 0 else 0
            # 涨跌幅 = (今日收盘价 - 前一日收盘价) / 前一日收盘价 * 100%
            change_percent = (change / prev_close * 100) if prev_close != 0 else 0

            # 涨跌表情
            trend_emoji = "📈" if change > 0 else "📉" if change < 0 else "➡️"

            message = f"""💼 黄金收盘价格播报 💼

📅 日期: {today}
🏅 品种: Au99.99 (上海黄金交易所)

💰 开盘价: {hist_data.get('open', 'N/A')} 元/克
💰 收盘价: {hist_data.get('close', 'N/A')} 元/克
📊 最高价: {hist_data.get('high', 'N/A')} 元/克
📊 最低价: {hist_data.get('low', 'N/A')} 元/克

📈 前一日收盘: {prev_close:.2f} 元/克
{trend_emoji} 涨跌额: {change:.2f} 元/克
{trend_emoji} 涨跌幅: {change_percent:.2f}%

⏰ 推送时间: {datetime.now().strftime('%H:%M:%S')}
📊 数据来源: 上海黄金交易所"""

            self.send_dingtalk_message(message)
            global service_status
            service_status['last_push'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            service_status['push_count'] += 1
        else:
            error_msg = f"""❌ 黄金价格数据获取失败

📅 日期: {datetime.now().strftime('%Y-%m-%d')}
⏰ 时间: {datetime.now().strftime('%H:%M:%S')}
🔧 请检查数据源连接"""
            self.send_dingtalk_message(error_msg)

    def test_push(self):
        """测试推送功能"""
        logger.info("测试钉钉推送功能...")
        test_message = f"""🧪 钉钉推送测试消息

⏰ 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔧 功能: 黄金价格推送服务
✅ 状态: 连接正常

如果您收到此消息，说明钉钉推送功能配置成功！"""

        result = self.send_dingtalk_message(test_message)
        if result:
            logger.info("✅ 钉钉推送测试成功")
            global service_status
            service_status['last_push'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            service_status['push_count'] += 1
        else:
            logger.error("❌ 钉钉推送测试失败")
        return result

# 初始化服务
gold_service = GoldService()

def run_scheduler():
    """运行定时任务调度器"""
    global scheduler_running
    logger.info("📅 定时推送调度器已启动")

    while scheduler_running:
        try:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次
        except Exception as e:
            logger.error(f"调度器运行异常: {e}")
            service_status['errors'].append({
                'time': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000'),
                'error': str(e)
            })

def start_scheduler():
    """启动定时任务"""
    global scheduler_thread, scheduler_running

    if scheduler_running:
        return False

    # 设置定时任务
    schedule.every().day.at("09:02").do(gold_service.push_opening_price)
    schedule.every().day.at("16:02").do(gold_service.push_closing_price)

    scheduler_running = True
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()

    service_status['running'] = True
    service_status['start_time'] = datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')

    logger.info("定时任务设置完成:")
    logger.info("- 每日09:02推送开盘价格（仅工作日）")
    logger.info("- 每日16:02推送收盘价格（仅工作日）")
    logger.info("- 周末将自动跳过推送")

    return True

def stop_scheduler():
    """停止定时任务"""
    global scheduler_running

    scheduler_running = False
    schedule.clear()

    service_status['running'] = False

    return True


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
def api_service_status():
    """获取服务状态"""
    return jsonify({
        'status': 'success',
        'running': service_status['running'],
        'start_time': service_status['start_time'],
        'last_push': service_status['last_push'],
        'push_count': service_status['push_count'],
        'errors': service_status['errors'][-5:],  # 最近5个错误
        'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
    })

@app.route('/api/service/start', methods=['POST'])
def api_start_service():
    """启动推送服务"""
    try:
        if service_status['running']:
            return jsonify({
                'status': 'warning',
                'message': '推送服务已在运行中',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            })

        success = start_scheduler()
        if success:
            logger.info("🚀 定时推送服务已启动")
            return jsonify({
                'status': 'success',
                'message': '定时推送服务启动成功',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '推送服务启动失败',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 500

    except Exception as e:
        logger.error(f"启动服务失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'启动失败: {str(e)}',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/service/stop', methods=['POST'])
def api_stop_service():
    """停止推送服务"""
    try:
        success = stop_scheduler()
        if success:
            logger.info("⏹️ 定时推送服务已停止")
            return jsonify({
                'status': 'success',
                'message': '定时推送服务已停止',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            })
        else:
            return jsonify({
                'status': 'error',
                'message': '推送服务停止失败',
                'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
            }), 500

    except Exception as e:
        logger.error(f"停止服务失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'停止失败: {str(e)}',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/push/test', methods=['POST'])
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
def api_push_opening():
    """推送开盘价"""
    try:
        gold_service.push_opening_price()

        return jsonify({
            'status': 'success',
            'message': '开盘价格推送成功',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        })

    except Exception as e:
        logger.error(f"开盘价推送失败: {e}")
        return jsonify({
            'status': 'error',
            'message': f'开盘价推送失败: {str(e)}',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        }), 500

@app.route('/api/push/closing', methods=['POST'])
def api_push_closing():
    """推送收盘价"""
    try:
        gold_service.push_closing_price()

        return jsonify({
            'status': 'success',
            'message': '收盘价格推送成功',
            'timestamp': datetime.now().strftime('%Y-%m-%dT%H:%M:%S.000')
        })

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
            'opening': '工作日 09:02',
            'closing': '工作日 16:02'
        },
        'data_source': '上海黄金交易所',
        'symbol': 'Au99.99'
    })

def create_dingtalk_config():
    """创建钉钉配置文件"""
    config = {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN",
        "description": "请将YOUR_ACCESS_TOKEN替换为您的钉钉机器人access_token"
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
    # 检查配置文件
    import os
    if not os.path.exists('dingtalk_config.json'):
        print("📝 首次运行，创建钉钉配置文件...")
        create_dingtalk_config()
        print("⚠️  请编辑 dingtalk_config.json 配置钉钉webhook地址")

    print("🏅 启动黄金价格服务...")
    print("🌐 服务地址: http://127.0.0.1:5080")
    print("📋 管理界面: http://127.0.0.1:5080")
    print("📖 API文档: http://127.0.0.1:5080/api/info")
    print("🔔 钉钉推送: 工作日 09:02 和 16:02")
    print("-" * 50)

    app.run(debug=True, host='0.0.0.0', port=5080)