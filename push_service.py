#!/usr/bin/env python3
"""
推送服务模块
提供通用的推送服务接口，支持多种推送方式的扩展
"""

import json
import logging
import requests
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from datetime import datetime

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

    def test_connection(self) -> bool:
        """测试钉钉连接"""
        test_message = f"""# 🧪 钉钉推送测试消息


**⏰ 测试时间:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

**🔧 功能:** 黄金价格推送服务

**✅ 状态:** 连接正常


如果您收到此消息，说明钉钉推送功能配置成功！


🔗 [管理界面]({self.link_url})"""

        return self.send_message(test_message, title="钉钉推送测试")


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
        """通过指定服务发送消息"""
        service = self.get_service(service_name)
        if not service:
            return False

        return service.send_message(message, **kwargs)

    def test_service(self, service_name: Optional[str] = None) -> bool:
        """测试指定服务"""
        service = self.get_service(service_name)
        if not service:
            return False

        return service.test_connection()

    def get_available_services(self) -> Dict[str, str]:
        """获取可用服务列表"""
        return {name: service.name for name, service in self.services.items()}


class MessageTemplate:
    """消息模板管理"""

    # 开盘价消息模板
    OPENING_PRICE_TEMPLATE = """# 📈 黄金开盘价格播报 📈


**📅 日期:** {date} {time}

**🏅 品种:** {symbol}

**💰 开盘价:** {open_price} 元/克

**📊 前日收盘:** {prev_close} 元/克

🔗 [查看详细数据]({link_url})"""

    # 收盘价消息模板
    CLOSING_PRICE_TEMPLATE = """# 💼 黄金收盘价格播报 💼


**📅 日期:** {date} {time}

**🏅 品种:** {symbol}

**💰 开-收盘价:** {open_price} ~ {close_price} 元/克

**📊 最低-高价:** {low_price} ~ {high_price} 元/克

**📈 前一日收盘:** {prev_close} 元/克

**{trend_emoji} 涨跌额:** {change:.2f} 元/克 ({change_percent:.2f}%)

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
    def format_error_message(cls, data: Dict[str, Any]) -> str:
        """格式化错误消息"""
        return cls.ERROR_MESSAGE_TEMPLATE.format(**data)


def create_push_service_manager() -> PushServiceManager:
    """创建并配置推送服务管理器"""
    manager = PushServiceManager()

    # 加载钉钉配置
    try:
        with open('dingtalk_config.json', 'r', encoding='utf-8') as f:
            dingtalk_config = json.load(f)

        # 注册钉钉推送服务
        dingtalk_service = DingTalkPushService(dingtalk_config)
        manager.register_service('dingtalk', dingtalk_service, is_default=True)

    except FileNotFoundError:
        logger.warning("钉钉配置文件不存在，跳过注册钉钉推送服务")
    except Exception as e:
        logger.error(f"加载钉钉配置失败: {e}")

    return manager


# 全局推送服务管理器实例
push_manager = create_push_service_manager()