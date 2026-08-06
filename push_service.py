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
import os
import tempfile
import time

import requests
from abc import ABC, abstractmethod
from pathlib import Path
from threading import RLock
from typing import Dict, Any, Optional
from urllib.parse import urlparse
from datetime import datetime
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

CHANNEL_NAMES = ('dingtalk', 'feishu')
RUNTIME_CONFIG_PATH_ENV = 'GOLD_PUSH_CONFIG_PATH'
DEFAULT_LINK_URL = 'http://127.0.0.1:5080'


def runtime_config_path() -> Path:
    """Return the server-owned persistence path for channel credentials."""
    return Path(os.environ.get(RUNTIME_CONFIG_PATH_ENV, 'data/push_channels.json'))


def _default_channel_configs() -> Dict[str, Dict[str, Any]]:
    return {
        'dingtalk': {
            'enabled': False,
            'webhook_url': '',
            'link_url': DEFAULT_LINK_URL,
        },
        'feishu': {
            'enabled': False,
            'webhook_url': '',
            'secret': '',
            'link_url': DEFAULT_LINK_URL,
        },
    }


def _merged_channel_configs(raw_config: Any) -> Dict[str, Dict[str, Any]]:
    """Normalise persisted or legacy channel config without exposing secrets."""
    configs = _default_channel_configs()
    if not isinstance(raw_config, dict):
        return configs

    for name in CHANNEL_NAMES:
        channel_config = raw_config.get(name)
        if isinstance(channel_config, dict):
            configs[name].update({
                key: value
                for key, value in channel_config.items()
                if key in configs[name]
            })
    return configs


def load_channel_configs() -> Dict[str, Dict[str, Any]]:
    """Load server-owned runtime settings saved through the admin UI."""
    config_path = runtime_config_path()
    try:
        with config_path.open('r', encoding='utf-8') as file:
            return _merged_channel_configs(json.load(file))
    except FileNotFoundError:
        return _default_channel_configs()
    except Exception as error:
        logger.error("加载推送渠道配置失败: %s", error)
        return _default_channel_configs()


def _validate_url(value: str, field_name: str, allowed_schemes: set[str]) -> None:
    parsed = urlparse(value)
    if parsed.scheme not in allowed_schemes or not parsed.netloc:
        raise ValueError(f'{field_name} 必须是有效的 {" / ".join(sorted(allowed_schemes))} 地址')


def update_channel_configs(changes: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Merge, validate and atomically persist admin-provided channel settings."""
    if not isinstance(changes, dict):
        raise ValueError('请求体必须是 JSON 对象')

    configs = load_channel_configs()
    for name in CHANNEL_NAMES:
        channel_changes = changes.get(name)
        if channel_changes is None:
            continue
        if not isinstance(channel_changes, dict):
            raise ValueError(f'{name} 配置必须是对象')

        config = configs[name]
        if 'enabled' in channel_changes:
            if not isinstance(channel_changes['enabled'], bool):
                raise ValueError(f'{name}.enabled 必须是布尔值')
            config['enabled'] = channel_changes['enabled']

        for key in ('webhook_url', 'link_url', 'secret'):
            if key not in channel_changes or key not in config:
                continue
            value = channel_changes[key]
            if not isinstance(value, str):
                raise ValueError(f'{name}.{key} 必须是字符串')
            if value:
                if key == 'webhook_url':
                    _validate_url(value, f'{name}.{key}', {'https'})
                elif key == 'link_url':
                    _validate_url(value, f'{name}.{key}', {'http', 'https'})
                config[key] = value

        for key in ('webhook_url', 'secret'):
            if channel_changes.get(f'clear_{key}'):
                config[key] = ''

        if config['enabled'] and not config['webhook_url']:
            raise ValueError(f'启用{name}推送前必须填写 Webhook 地址')

    config_path = runtime_config_path()
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor, temporary_path = tempfile.mkstemp(
        prefix='.push-channels-',
        suffix='.json',
        dir=config_path.parent,
        text=True,
    )
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as file:
            json.dump(configs, file, ensure_ascii=False, indent=2)
            file.write('\n')
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, config_path)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)

    return configs


def public_channel_configs() -> Dict[str, Dict[str, Any]]:
    """Expose editable non-secret metadata to the authenticated admin UI."""
    configs = load_channel_configs()
    return {
        'dingtalk': {
            'enabled': configs['dingtalk']['enabled'],
            'webhook_configured': bool(configs['dingtalk']['webhook_url']),
            'link_url': configs['dingtalk']['link_url'],
        },
        'feishu': {
            'enabled': configs['feishu']['enabled'],
            'webhook_configured': bool(configs['feishu']['webhook_url']),
            'secret_configured': bool(configs['feishu']['secret']),
            'link_url': configs['feishu']['link_url'],
        },
    }


def primary_link_url() -> str:
    """Return the first enabled channel's management URL for market messages."""
    configs = load_channel_configs()
    for name in CHANNEL_NAMES:
        config = configs[name]
        if config['enabled'] and config['webhook_url']:
            return config['link_url']
    return DEFAULT_LINK_URL


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

    def __init__(self, config_loader=None):
        self.services: Dict[str, PushServiceBase] = {}
        self.default_service: Optional[str] = None
        self._config_loader = config_loader
        self._lock = RLock()

    def reload_services(self) -> None:
        """Refresh services from persistent configuration before each delivery."""
        if not self._config_loader:
            return

        configs = self._config_loader()
        services: Dict[str, PushServiceBase] = {}
        service_types = {
            'dingtalk': DingTalkPushService,
            'feishu': FeishuPushService,
        }
        for name in CHANNEL_NAMES:
            config = configs[name]
            if config.get('enabled') and config.get('webhook_url'):
                services[name] = service_types[name](config)

        with self._lock:
            self.services = services
            self.default_service = next(iter(services), None)

    def register_service(self, name: str, service: PushServiceBase, is_default: bool = False):
        """注册推送服务"""
        with self._lock:
            self.services[name] = service
            if is_default or not self.default_service:
                self.default_service = name
        logger.info(f"注册推送服务: {name}")

    def get_service(self, name: Optional[str] = None) -> Optional[PushServiceBase]:
        """获取推送服务"""
        self.reload_services()
        with self._lock:
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
        self.reload_services()
        if service_name:
            with self._lock:
                service = self.services.get(service_name)
            return bool(service and service.send_message(message, **kwargs))

        with self._lock:
            services = dict(self.services)
        if not services:
            logger.error("没有已配置的推送服务")
            return False

        results = {
            name: service.send_message(message, **kwargs)
            for name, service in services.items()
        }
        failed_services = [name for name, success in results.items() if not success]
        if failed_services:
            logger.error("以下推送服务发送失败: %s", ', '.join(failed_services))
        return all(results.values())

    def test_service(self, service_name: Optional[str] = None) -> bool:
        """测试服务；未指定渠道时测试全部已注册服务。"""
        self.reload_services()
        if service_name:
            with self._lock:
                service = self.services.get(service_name)
            return bool(service and service.test_connection())

        with self._lock:
            services = dict(self.services)
        if not services:
            logger.error("没有已配置的推送服务")
            return False

        results = {
            name: service.test_connection()
            for name, service in services.items()
        }
        failed_services = [name for name, success in results.items() if not success]
        if failed_services:
            logger.error("以下推送服务测试失败: %s", ', '.join(failed_services))
        return all(results.values())

    def get_available_services(self) -> Dict[str, str]:
        """获取可用服务列表"""
        self.reload_services()
        with self._lock:
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
    manager = PushServiceManager(config_loader=load_channel_configs)
    manager.reload_services()

    return manager


# 全局推送服务管理器实例
push_manager = create_push_service_manager()
