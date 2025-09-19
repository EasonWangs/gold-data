#!/usr/bin/env python3
"""
测试黄金价格API的客户端
"""

import requests
import json
from datetime import datetime

def test_api_endpoints():
    base_url = "http://127.0.0.1:5080"

    print("🧪 测试黄金价格API接口")
    print("=" * 50)

    # 测试API信息接口
    print("\n1. 测试API信息接口:")
    try:
        response = requests.get(f"{base_url}/api/gold/info")
        if response.status_code == 200:
            data = response.json()
            print("✅ API信息获取成功")
            print(f"   服务名称: {data.get('name')}")
            print(f"   版本: {data.get('version')}")
            print(f"   数据源: {data.get('data_source')}")
        else:
            print(f"❌ 请求失败: {response.status_code}")
    except Exception as e:
        print(f"❌ 请求异常: {e}")

    # 测试实时价格接口
    print("\n2. 测试实时黄金价格接口:")
    try:
        response = requests.get(f"{base_url}/api/gold/spot_quotations_sge")
        if response.status_code == 200:
            data = response.json()
            print("✅ 实时价格获取成功")
            print(f"   状态: {data.get('status')}")
            print(f"   数据条数: {data.get('count')}")
            if data.get('data'):
                print("   最新价格数据:")
                for item in data['data'][:3]:  # 只显示前3条
                    print(f"     品种: {item.get('品种', 'N/A')}")
                    print(f"     最新价: {item.get('最新价', 'N/A')}")
                    print(f"     涨跌: {item.get('涨跌', 'N/A')}")
                    print()
        else:
            print(f"❌ 请求失败: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"❌ 请求异常: {e}")

    # 测试历史价格接口
    print("\n3. 测试历史价格接口 (最近5天):")
    try:
        response = requests.get(f"{base_url}/api/gold/spot_hist_sge?days=5")
        if response.status_code == 200:
            data = response.json()
            print("✅ 历史价格获取成功")
            print(f"   状态: {data.get('status')}")
            print(f"   天数: {data.get('days')}")
            print(f"   数据条数: {data.get('count')}")
            if data.get('data'):
                print("   历史价格数据:")
                for item in data['data'][-3:]:  # 显示最近3天
                    print(f"     日期: {item.get('date', 'N/A')}")
                    print(f"     开盘: {item.get('open', 'N/A')}")
                    print(f"     收盘: {item.get('close', 'N/A')}")
                    print(f"     最高: {item.get('high', 'N/A')}")
                    print(f"     最低: {item.get('low', 'N/A')}")
                    print()
        else:
            print(f"❌ 请求失败: {response.status_code}")
            print(response.text)
    except Exception as e:
        print(f"❌ 请求异常: {e}")

    # 测试首页
    print("\n4. 测试首页:")
    try:
        response = requests.get(f"{base_url}/")
        if response.status_code == 200:
            data = response.json()
            print("✅ 首页访问成功")
            print(f"   消息: {data.get('message')}")
        else:
            print(f"❌ 请求失败: {response.status_code}")
    except Exception as e:
        print(f"❌ 请求异常: {e}")

if __name__ == "__main__":
    test_api_endpoints()