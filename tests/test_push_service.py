import base64
import hashlib
import hmac
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from push_service import (  # noqa: E402
    FeishuPushService,
    PushServiceManager,
    create_push_service_manager,
)


class FeishuPushServiceTests(unittest.TestCase):
    @patch('push_service.time.time', return_value=1_700_000_000)
    @patch('push_service.requests.post')
    def test_markdown_message_uses_card_and_optional_signature(self, post, _time):
        response = Mock(status_code=200)
        response.json.return_value = {'StatusCode': 0}
        post.return_value = response
        service = FeishuPushService({
            'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/test',
            'secret': 'signing-secret',
        })

        self.assertTrue(service.send_message('# 行情', title='黄金价格播报'))

        payload = post.call_args.kwargs['json']
        self.assertEqual(payload['msg_type'], 'interactive')
        self.assertEqual(payload['card']['header']['title']['content'], '黄金价格播报')
        self.assertEqual(payload['card']['body']['elements'][0]['content'], '# 行情')
        self.assertEqual(payload['timestamp'], '1700000000')
        expected_sign = base64.b64encode(
            hmac.new(
                key=b'1700000000\nsigning-secret',
                msg=b'',
                digestmod=hashlib.sha256,
            ).digest()
        ).decode('utf-8')
        self.assertEqual(payload['sign'], expected_sign)

    @patch('push_service.requests.post')
    def test_text_message_accepts_lowercase_success_code(self, post):
        response = Mock(status_code=200)
        response.json.return_value = {'code': 0}
        post.return_value = response
        service = FeishuPushService({
            'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/test',
        })

        self.assertTrue(service.send_message('纯文本', use_markdown=False))
        self.assertEqual(post.call_args.kwargs['json'], {
            'msg_type': 'text',
            'content': {'text': '纯文本'},
        })


class PushServiceManagerTests(unittest.TestCase):
    def test_factory_registers_enabled_feishu_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / 'feishu_config.json'
            config_path.write_text(json.dumps({
                'enabled': True,
                'webhook_url': 'https://open.feishu.cn/open-apis/bot/v2/hook/test',
            }), encoding='utf-8')
            previous_directory = os.getcwd()
            try:
                os.chdir(directory)
                manager = create_push_service_manager()
            finally:
                os.chdir(previous_directory)

        self.assertEqual(manager.get_available_services(), {
            'feishu': 'FeishuPushService',
        })

    def test_unspecified_service_broadcasts_to_every_enabled_channel(self):
        manager = PushServiceManager()
        dingtalk = Mock()
        feishu = Mock()
        dingtalk.send_message.return_value = True
        feishu.send_message.return_value = True
        manager.register_service('dingtalk', dingtalk)
        manager.register_service('feishu', feishu)

        self.assertTrue(manager.send_message('行情', title='测试'))
        dingtalk.send_message.assert_called_once_with('行情', title='测试')
        feishu.send_message.assert_called_once_with('行情', title='测试')

    def test_explicit_service_name_still_targets_one_channel(self):
        manager = PushServiceManager()
        dingtalk = Mock()
        feishu = Mock()
        feishu.send_message.return_value = True
        manager.register_service('dingtalk', dingtalk)
        manager.register_service('feishu', feishu)

        self.assertTrue(manager.send_message('行情', service_name='feishu'))
        feishu.send_message.assert_called_once_with('行情')
        dingtalk.send_message.assert_not_called()

    def test_broadcast_fails_when_any_channel_fails(self):
        manager = PushServiceManager()
        dingtalk = Mock()
        feishu = Mock()
        dingtalk.send_message.return_value = True
        feishu.send_message.return_value = False
        manager.register_service('dingtalk', dingtalk)
        manager.register_service('feishu', feishu)

        self.assertFalse(manager.send_message('行情'))


if __name__ == '__main__':
    unittest.main()
