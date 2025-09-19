#!/usr/bin/env python3
"""
钉钉推送功能
定时推送黄金价格信息到钉钉群
"""

import requests
import json
import akshare as ak
import schedule
import time
from datetime import datetime, date
import threading
import logging

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class DingTalkPush:
    def __init__(self, webhook_url=None):
        """
        初始化钉钉推送
        :param webhook_url: 钉钉机器人webhook地址
        """
        self.webhook_url = webhook_url or self.load_webhook_config()

    def load_webhook_config(self):
        """
        从配置文件加载webhook地址
        """
        try:
            with open('dingtalk_config.json', 'r', encoding='utf-8') as f:
                config = json.load(f)
                return config.get('webhook_url')
        except FileNotFoundError:
            logger.warning("配置文件 dingtalk_config.json 不存在，请手动配置webhook地址")
            return None
        except Exception as e:
            logger.error(f"加载配置文件失败: {e}")
            return None

    def get_gold_price_data(self):
        """
        获取黄金价格数据
        """
        try:
            # 获取实时价格数据
            spot_data = ak.spot_quotations_sge(symbol="Au99.99")
            latest = None
            if not spot_data.empty:
                latest = spot_data.iloc[-1] if len(spot_data) > 0 else spot_data.iloc[0]

            # 获取历史数据
            hist_data = ak.spot_hist_sge(symbol='Au99.99')
            today_data = None
            prev_data = None
            if not hist_data.empty:
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
        """
        发送钉钉消息
        """
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
        """
        判断是否为交易日（排除周六周日）
        """
        today = datetime.now().weekday()  # 0=Monday, 6=Sunday
        return today < 5  # Monday(0) to Friday(4)

    def push_opening_price(self):
        """
        推送开盘价格 (9:02 AM)
        """
        if not self.is_trading_day():
            logger.info("今日为周末，跳过开盘价格推送")
            return

        logger.info("开始推送开盘价格...")
        data = self.get_gold_price_data()

        if data:
            today = datetime.now().strftime('%Y-%m-%d')
            hist_data = data['history']

            message = f"""📈 黄金开盘价格播报 📈

📅 日期: {today}
🏅 品种: Au99.99 (上海黄金交易所)
💰 开盘价: {hist_data.get('open', 'N/A')} 元/克

⏰ 推送时间: {datetime.now().strftime('%H:%M:%S')}
📊 数据来源: 上海黄金交易所"""

            self.send_dingtalk_message(message)
        else:
            error_msg = f"""❌ 黄金价格数据获取失败

📅 日期: {datetime.now().strftime('%Y-%m-%d')}
⏰ 时间: {datetime.now().strftime('%H:%M:%S')}
🔧 请检查数据源连接"""

            self.send_dingtalk_message(error_msg)

    def push_closing_price(self):
        """
        推送收盘价格 (4:02 PM)
        """
        if not self.is_trading_day():
            logger.info("今日为周末，跳过收盘价格推送")
            return

        logger.info("开始推送收盘价格...")
        data = self.get_gold_price_data()

        if data:
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
        else:
            error_msg = f"""❌ 黄金价格数据获取失败

📅 日期: {datetime.now().strftime('%Y-%m-%d')}
⏰ 时间: {datetime.now().strftime('%H:%M:%S')}
🔧 请检查数据源连接"""

            self.send_dingtalk_message(error_msg)

    def setup_schedule(self):
        """
        设置定时任务
        """
        # 每日9:02推送开盘价（工作日）
        schedule.every().day.at("09:02").do(self.push_opening_price)

        # 每日16:02推送收盘价（工作日）
        schedule.every().day.at("16:02").do(self.push_closing_price)

        logger.info("定时任务设置完成:")
        logger.info("- 每日09:02推送开盘价格（仅工作日）")
        logger.info("- 每日16:02推送收盘价格（仅工作日）")
        logger.info("- 周末将自动跳过推送")

    def run_scheduler(self):
        """
        运行定时任务调度器
        """
        logger.info("启动定时推送服务...")
        while True:
            schedule.run_pending()
            time.sleep(60)  # 每分钟检查一次

    def test_push(self):
        """
        测试推送功能
        """
        logger.info("测试钉钉推送功能...")
        test_message = f"""🧪 钉钉推送测试消息

⏰ 测试时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
🔧 功能: 黄金价格推送服务
✅ 状态: 连接正常

如果您收到此消息，说明钉钉推送功能配置成功！"""

        result = self.send_dingtalk_message(test_message)
        if result:
            logger.info("✅ 钉钉推送测试成功")
        else:
            logger.error("❌ 钉钉推送测试失败")
        return result

def create_config_file():
    """
    创建配置文件模板
    """
    config = {
        "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN",
        "description": "请将YOUR_ACCESS_TOKEN替换为您的钉钉机器人access_token"
    }

    try:
        with open('dingtalk_config.json', 'w', encoding='utf-8') as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        print("✅ 配置文件模板已创建: dingtalk_config.json")
        print("📝 请编辑配置文件，填入正确的webhook地址")
        return True
    except Exception as e:
        print(f"❌ 创建配置文件失败: {e}")
        return False

def main():
    """
    主函数
    """
    print("🔔 钉钉黄金价格推送服务")
    print("=" * 50)

    # 检查配置文件
    try:
        with open('dingtalk_config.json', 'r') as f:
            config = json.load(f)
            if config.get('webhook_url', '').find('YOUR_ACCESS_TOKEN') != -1:
                print("⚠️  请先配置钉钉webhook地址")
                return
    except FileNotFoundError:
        print("📝 首次运行，创建配置文件...")
        if create_config_file():
            print("⚠️  请先配置钉钉webhook地址后重新运行")
            return

    # 初始化推送服务
    push_service = DingTalkPush()

    print("\n选择操作:")
    print("1. 测试钉钉推送")
    print("2. 手动推送开盘价格")
    print("3. 手动推送收盘价格")
    print("4. 启动定时推送服务")
    print("5. 退出")

    while True:
        try:
            choice = input("\n请选择 (1-5): ").strip()

            if choice == "1":
                push_service.test_push()
            elif choice == "2":
                push_service.push_opening_price()
            elif choice == "3":
                push_service.push_closing_price()
            elif choice == "4":
                push_service.setup_schedule()
                print("🚀 定时推送服务已启动...")
                print("💡 按 Ctrl+C 停止服务")
                try:
                    push_service.run_scheduler()
                except KeyboardInterrupt:
                    print("\n👋 定时推送服务已停止")
                    break
            elif choice == "5":
                print("👋 程序退出")
                break
            else:
                print("❌ 无效选择，请重新输入")

        except KeyboardInterrupt:
            print("\n👋 程序退出")
            break
        except Exception as e:
            print(f"❌ 发生错误: {e}")

if __name__ == "__main__":
    main()