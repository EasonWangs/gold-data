# 推送服务使用说明

## 概述

推送功能已重构为通用的推送服务架构，支持多种推送方式的扩展和配置。

## 架构设计

### 1. 推送服务基类 (`PushServiceBase`)
- 抽象基类，定义推送服务的通用接口
- 支持消息模板格式化
- 提供连接测试功能

### 2. 钉钉推送服务 (`DingTalkPushService`)
- 继承推送服务基类
- 支持 Markdown 和文本消息格式
- 自动处理钉钉 API 调用和错误处理

### 3. 推送服务管理器 (`PushServiceManager`)
- 统一管理多个推送服务
- 支持服务注册和动态选择
- 提供统一的消息发送接口

### 4. 消息模板管理 (`MessageTemplate`)
- 预定义消息模板（开盘价、收盘价、错误信息）
- 支持参数化消息生成
- 格式化消息数据

## 使用方式

### 基础用法

```python
from push_service import push_manager, MessageTemplate

# 发送消息
push_manager.send_message("测试消息", service_name="dingtalk")

# 测试服务
push_manager.test_service("dingtalk")

# 使用模板发送消息
data = {
    'date': '2025-09-26',
    'time': '09:00:00',
    'symbol': 'Au99.99',
    'open_price': '856.50',
    'prev_close': '856.63',
    'link_url': 'http://127.0.0.1:5080'
}
message = MessageTemplate.format_opening_price_message(data)
push_manager.send_message(message)
```

### 添加新的推送服务

1. 创建推送服务类：

```python
class WeChatPushService(PushServiceBase):
    def __init__(self, config):
        super().__init__(config)
        self.app_id = config.get('app_id')
        self.secret = config.get('secret')

    def send_message(self, message, **kwargs):
        # 实现微信推送逻辑
        pass

    def test_connection(self):
        # 实现连接测试逻辑
        pass
```

2. 注册服务：

```python
# 在 push_service.py 的 create_push_service_manager 函数中添加
wechat_config = load_wechat_config()
wechat_service = WeChatPushService(wechat_config)
manager.register_service('wechat', wechat_service)
```

### 配置推送规则

编辑 `push_config.json` 文件：

```json
{
  "push_services": {
    "dingtalk": {
      "enabled": true,
      "name": "钉钉推送",
      "config_file": "dingtalk_config.json"
    },
    "wechat": {
      "enabled": true,
      "name": "微信推送",
      "config_file": "wechat_config.json"
    }
  },
  "push_rules": {
    "opening_price": {
      "enabled": true,
      "time": "09:00",
      "weekdays_only": true,
      "services": ["dingtalk", "wechat"],
      "template": "opening_price"
    },
    "price_alert": {
      "enabled": true,
      "condition": "price_change > 5%",
      "services": ["dingtalk"],
      "template": "price_alert"
    }
  }
}
```

### 自定义消息模板

在 `MessageTemplate` 类中添加新模板：

```python
PRICE_ALERT_TEMPLATE = """# 🚨 黄金价格预警 🚨

**当前价格:** {current_price} 元/克
**变动幅度:** {change_percent}%
**预警条件:** {alert_condition}

🔗 [查看详情]({link_url})"""

@classmethod
def format_price_alert_message(cls, data):
    return cls.PRICE_ALERT_TEMPLATE.format(**data)
```

## 扩展示例

### 添加邮件推送服务

```python
import smtplib
from email.mime.text import MIMEText

class EmailPushService(PushServiceBase):
    def __init__(self, config):
        super().__init__(config)
        self.smtp_server = config.get('smtp_server')
        self.smtp_port = config.get('smtp_port', 587)
        self.username = config.get('username')
        self.password = config.get('password')
        self.recipients = config.get('recipients', [])

    def send_message(self, message, subject="黄金价格通知", **kwargs):
        try:
            msg = MIMEText(message, 'plain', 'utf-8')
            msg['Subject'] = subject
            msg['From'] = self.username

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                server.starttls()
                server.login(self.username, self.password)
                for recipient in self.recipients:
                    msg['To'] = recipient
                    server.send_message(msg)
            return True
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")
            return False

    def test_connection(self):
        test_message = "推送服务测试邮件"
        return self.send_message(test_message, "测试邮件")
```

### 添加 Slack 推送服务

```python
import requests

class SlackPushService(PushServiceBase):
    def __init__(self, config):
        super().__init__(config)
        self.webhook_url = config.get('webhook_url')
        self.channel = config.get('channel', '#general')
        self.username = config.get('username', '黄金价格机器人')

    def send_message(self, message, **kwargs):
        payload = {
            'text': message,
            'channel': self.channel,
            'username': self.username,
            'icon_emoji': ':chart_with_upwards_trend:'
        }

        try:
            response = requests.post(self.webhook_url, json=payload)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"Slack消息发送失败: {e}")
            return False

    def test_connection(self):
        test_message = "🧪 Slack 推送服务测试消息"
        return self.send_message(test_message)
```

## 优势

1. **可扩展性**：轻松添加新的推送服务类型
2. **配置灵活**：支持动态配置推送规则和服务
3. **模板化**：统一的消息模板管理
4. **错误处理**：完善的异常处理和日志记录
5. **测试支持**：内置连接测试功能
6. **解耦设计**：推送逻辑与业务逻辑分离

## 文件结构

```
├── push_service.py          # 推送服务核心模块
├── push_config.json         # 推送配置文件
├── dingtalk_config.json     # 钉钉配置文件
├── gold_service.py          # 黄金服务（已重构）
└── README_PUSH_SERVICE.md   # 推送服务使用说明
```

## 注意事项

1. 确保配置文件权限安全，避免敏感信息泄露
2. 推送服务异常不会影响主业务逻辑
3. 建议定期测试推送服务连接状态
4. 新增推送服务需要实现 `send_message` 和 `test_connection` 方法
5. 消息模板支持中文，注意字符编码处理