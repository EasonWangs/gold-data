#!/usr/bin/env python3
"""
推送服务模块
提供通用的推送服务接口，支持多种推送方式的扩展
"""

import base64
import hashlib
import hmac
import json
import logging
import time

import requests
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


class PushServiceBase(ABC):
    """推送服务基类"""

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.name = self.__class__.__name__

    @abstractmethod
    def send_message(self, message: str, **kwargs) -> bool:
        """发送消息的抽象方法"""
        pass

    @abstractmethod
    def test_connection(self) -> bool:
        """测试连接的抽象方法"""
        pass

    def format_message(self, template: str, data: Dict[str, Any]) -> str:
        """格式化消息模板"""
        try:
            return template.format(**data)
        except KeyError as e:
            logger.error(f"消息模板格式化失败，缺少变量: {e}")
            return template
        except Exception as e:
            logger.error(f"消息模板格式化失败: {e}")
            return template


class DingTalkPushService(PushServiceBase):
    """钉钉推送服务"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.webhook_url = config.get('webhook_url')
        self.link_url = config.get('link_url', 'http://127.0.0.1:5080')

        if not self.webhook_url:
            logger.warning("钉钉webhook地址未配置")

    def send_message(self, message: str, use_markdown: bool = True, **kwargs) -> bool:
        """发送钉钉消息"""
        if not self.webhook_url:
            logger.error("webhook地址未配置，无法发送消息")
            return False

        headers = {'Content-Type': 'application/json'}

        if use_markdown:
            data = {
                "msgtype": "markdown",
                "markdown": {
                    "title": kwargs.get('title', '黄金价格播报'),
                    "text": message
                }
            }
        else:
            data = {
                "msgtype": "text",
                "text": {
                    "content": message
                }
            }

        try:
            response = requests.post(
                self.webhook_url,
                headers=headers,
                data=json.dumps(data),
                timeout=10,
            )
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

    def test_connection(self) -> bool:
        """测试钉钉连接"""
        test_message = f"""# 🧪 钉钉推送测试消息


**⏰ 测试时间:** {datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）

**🔧 功能:** 黄金价格推送服务

**✅ 状态:** 连接正常


如果您收到此消息，说明钉钉推送功能配置成功！


🔗 [管理界面]({self.link_url})"""

        return self.send_message(test_message, title="钉钉推送测试")


class FeishuPushService(PushServiceBase):
    """飞书群自定义机器人推送服务。"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.webhook_url = config.get('webhook_url')
        self.secret = config.get('secret')
        self.link_url = config.get('link_url', 'http://127.0.0.1:5080')

        if not self.webhook_url:
            logger.warning("飞书 webhook 地址未配置")

    def _build_signature(self) -> Dict[str, str]:
        """Build the optional signature required by Feishu webhook security."""
        if not self.secret:
            return {}

        timestamp = str(int(time.time()))
        string_to_sign = f'{timestamp}\n{self.secret}'
        signature = base64.b64encode(
            hmac.new(
                key=string_to_sign.encode('utf-8'),
                msg=b'',
                digestmod=hashlib.sha256,
            ).digest()
        ).decode('utf-8')
        return {'timestamp': timestamp, 'sign': signature}

    def send_message(self, message: str, use_markdown: bool = True, **kwargs) -> bool:
        """发送飞书消息卡片，保留项目现有 Markdown 快报格式。"""
        if not self.webhook_url:
            logger.error("飞书 webhook 地址未配置，无法发送消息")
            return False

        title = kwargs.get('title', '黄金价格播报')
        if use_markdown:
            data = {
                'msg_type': 'interactive',
                'card': {
                    'schema': '2.0',
                    'header': {
                        'title': {'tag': 'plain_text', 'content': title},
                        'template': 'blue',
                    },
                    'body': {
                        'direction': 'vertical',
                        'padding': '12px 12px 12px 12px',
                        'elements': [
                            {
                                'tag': 'markdown',
                                'content': message,
                                'text_align': 'left',
                                'text_size': 'normal_v2',
                                'margin': '0px 0px 0px 0px',
                            }
                        ],
                    },
                },
            }
        else:
            data = {
                'msg_type': 'text',
                'content': {'text': message},
            }

        data.update(self._build_signature())

        try:
            response = requests.post(
                self.webhook_url,
                headers={'Content-Type': 'application/json'},
                json=data,
                timeout=10,
            )
            if response.status_code != 200:
                logger.error("飞书消息发送失败: HTTP %s", response.status_code)
                return False

            result = response.json()
            status_code = result.get('StatusCode', result.get('code'))
            if status_code == 0:
                logger.info("飞书消息发送成功")
                return True

            error_message = result.get('StatusMessage', result.get('msg', result))
            logger.error("飞书消息发送失败: %s", error_message)
            return False
        except Exception as e:
            logger.error("发送飞书消息异常: %s", e)
            return False

    def test_connection(self) -> bool:
        """测试飞书连接。"""
        test_message = f"""# 🧪 飞书推送测试消息

**⏰ 测试时间:** {datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai）

**🔧 功能:** 黄金价格推送服务

**✅ 状态:** 连接正常

如果您收到此消息，说明飞书推送功能配置成功！

🔗 [管理界面]({self.link_url})"""
        return self.send_message(test_message, title='飞书推送测试')


class PushServiceManager:
    """推送服务管理器"""

    def __init__(self):
        self.services: Dict[str, PushServiceBase] = {}
        self.default_service: Optional[str] = None

    def register_service(self, name: str, service: PushServiceBase, is_default: bool = False):
        """注册推送服务"""
        self.services[name] = service
        if is_default or not self.default_service:
            self.default_service = name
        logger.info(f"注册推送服务: {name}")

    def get_service(self, name: Optional[str] = None) -> Optional[PushServiceBase]:
        """获取推送服务"""
        service_name = name or self.default_service
        if not service_name:
            logger.error("未指定推送服务且无默认服务")
            return None

        service = self.services.get(service_name)
        if not service:
            logger.error(f"推送服务不存在: {service_name}")
            return None

        return service

    def send_message(self, message: str, service_name: Optional[str] = None, **kwargs) -> bool:
        """发送消息；未指定渠道时广播到全部已注册服务。"""
        if service_name:
            service = self.get_service(service_name)
            return bool(service and service.send_message(message, **kwargs))

        if not self.services:
            logger.error("没有已配置的推送服务")
            return False

        results = {
            name: service.send_message(message, **kwargs)
            for name, service in self.services.items()
        }
        failed_services = [name for name, success in results.items() if not success]
        if failed_services:
            logger.error("以下推送服务发送失败: %s", ', '.join(failed_services))
        return all(results.values())

    def test_service(self, service_name: Optional[str] = None) -> bool:
        """测试服务；未指定渠道时测试全部已注册服务。"""
        if service_name:
            service = self.get_service(service_name)
            return bool(service and service.test_connection())

        if not self.services:
            logger.error("没有已配置的推送服务")
            return False

        results = {
            name: service.test_connection()
            for name, service in self.services.items()
        }
        failed_services = [name for name, success in results.items() if not success]
        if failed_services:
            logger.error("以下推送服务测试失败: %s", ', '.join(failed_services))
        return all(results.values())

    def get_available_services(self) -> Dict[str, str]:
        """获取可用服务列表"""
        return {name: service.name for name, service in self.services.items()}


class MessageTemplate:
    """消息模板管理"""

    # 开盘价消息模板
    OPENING_PRICE_TEMPLATE = """# 📈 黄金{session_label}开盘播报


**📅 所属交易日:** {date}

**⏱ 首个有效报价:** {time}（Asia/Shanghai）

**🏅 品种:** {symbol}

**💰 开盘价:** {open_price} 元/克

**📊 前日收盘:** {prev_close} 元/克

**{trend_emoji} 涨跌额:** {change:.2f} 元/克 ({change_percent:.2f}%)

🔗 [查看详细数据]({link_url})"""

    # 收盘价消息模板
    CLOSING_PRICE_TEMPLATE = """# 💼 黄金日线收盘播报


**📅 交易日:** {date}

**⏱ 推送时间:** {time}（Asia/Shanghai）

**🏅 品种:** {symbol}

**💰 开盘价:** {open_price} 元/克

**📊 最低-高价:** {low_price} ~ {high_price} 元/克

**💰 收盘价:** {close_price} 元/克

**📈 前日收盘:** {prev_close} 元/克

**{trend_emoji} 涨跌额:** {change:.2f} 元/克 ({change_percent:.2f}%)

{simulation_note}
> 日线高低开收包含此前夜盘与当日日盘；并非银行积存金报价。

🔗 [查看详细数据]({link_url})"""

    STRATEGY_SIGNAL_TEMPLATE = """# 🚨 黄金策略交易信号

## ⚠️ 收盘确认，请重点关注

**📅 交易日:** {date}

**💰 确认收盘价:** {close_price:.2f} 元/克

---

{signals}

{simulation_note}
> 策略信号基于上金所 Au99.99 日线计算，仅供研究，不构成投资建议或实际交易指令。

🔗 [查看详细数据]({link_url})"""

    # 错误消息模板
    ERROR_MESSAGE_TEMPLATE = """# ❌ 黄金价格数据获取失败


**📅 日期:** {date}

**⏰ 时间:** {time}

**🔧 状态:** 请检查数据源连接


🔗 [服务状态]({link_url})"""

    @classmethod
    def format_opening_price_message(cls, data: Dict[str, Any]) -> str:
        """格式化开盘价消息"""
        return cls.OPENING_PRICE_TEMPLATE.format(**data)

    @classmethod
    def format_closing_price_message(cls, data: Dict[str, Any]) -> str:
        """格式化收盘价消息"""
        return cls.CLOSING_PRICE_TEMPLATE.format(**data)

    @classmethod
    def format_strategy_signal_message(cls, data: Dict[str, Any]) -> str:
        """Format the independent, high-visibility strategy signal alert."""
        return cls.STRATEGY_SIGNAL_TEMPLATE.format(**data)

    @classmethod
    def format_error_message(cls, data: Dict[str, Any]) -> str:
        """格式化错误消息"""
        return cls.ERROR_MESSAGE_TEMPLATE.format(**data)


def create_push_service_manager() -> PushServiceManager:
    """创建并配置推送服务管理器"""
    manager = PushServiceManager()

    service_configs = (
        ('dingtalk', 'dingtalk_config.json', DingTalkPushService, '钉钉'),
        ('feishu', 'feishu_config.json', FeishuPushService, '飞书'),
    )
    for name, filename, service_type, display_name in service_configs:
        try:
            with open(filename, 'r', encoding='utf-8') as f:
                config = json.load(f)
        except FileNotFoundError:
            logger.info("%s 配置文件不存在，跳过注册%s推送服务", filename, display_name)
            continue
        except Exception as e:
            logger.error("加载%s配置失败: %s", display_name, e)
            continue

        if not config.get('enabled', True):
            logger.info("%s 推送已禁用", display_name)
            continue
        if not config.get('webhook_url'):
            logger.warning("%s webhook 地址未配置，跳过注册%s推送服务", display_name, display_name)
            continue

        manager.register_service(name, service_type(config), is_default=manager.default_service is None)

    return manager


# 全局推送服务管理器实例
push_manager = create_push_service_manager()
