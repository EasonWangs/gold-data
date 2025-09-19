#!/usr/bin/env python3
"""
测试钉钉推送功能
"""

from dingtalk_push import DingTalkPush

def test_push():
    """测试推送功能"""
    push_service = DingTalkPush()

    print("🧪 测试钉钉推送功能")
    print("=" * 50)

    # 测试连接
    print("1. 测试钉钉连接...")
    result = push_service.test_push()

    if result:
        print("✅ 钉钉连接测试成功")

        print("\n2. 测试收盘价格推送...")
        push_service.push_closing_price()

        print("\n3. 测试开盘价格推送...")
        push_service.push_opening_price()

    else:
        print("❌ 钉钉连接测试失败，请检查配置")

if __name__ == "__main__":
    test_push()