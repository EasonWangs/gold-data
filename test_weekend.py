#!/usr/bin/env python3
"""
测试周末排除功能
"""

from datetime import datetime
from dingtalk_push import DingTalkPush

def test_weekend_check():
    """测试周末检查功能"""
    push_service = DingTalkPush()

    print("📅 周末排除功能测试")
    print("=" * 40)

    # 检查今天是否为交易日
    is_trading = push_service.is_trading_day()
    today = datetime.now()
    weekday = today.weekday()
    weekday_names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

    print(f"今天是: {today.strftime('%Y-%m-%d')} {weekday_names[weekday]}")
    print(f"是否为交易日: {'是' if is_trading else '否'}")
    print(f"weekday() 值: {weekday} (0=周一, 6=周日)")

    if is_trading:
        print("✅ 今天是工作日，推送功能正常运行")
    else:
        print("⏸️ 今天是周末，推送功能将跳过")

    print("\n测试推送功能:")
    print("- 开盘价格推送:")
    push_service.push_opening_price()

    print("- 收盘价格推送:")
    push_service.push_closing_price()

if __name__ == "__main__":
    test_weekend_check()