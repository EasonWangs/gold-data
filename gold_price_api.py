#!/usr/bin/env python3
"""
黄金价格 API 服务
提供实时黄金价格和历史价格数据接口
使用 akshare 库获取上海黄金交易所数据
"""

import akshare as ak
from flask import Flask, jsonify, request
from datetime import datetime
import pandas as pd

app = Flask(__name__)

def get_real_time_gold_price():
    """
    获取实时黄金价格
    """
    try:
        spot_quotations_sge_df = ak.spot_quotations_sge(symbol="Au99.99")
        return spot_quotations_sge_df
    except Exception as e:
        print(f"获取实时黄金价格失败: {e}")
        return None

def get_historical_gold_price(days=30):
    """
    获取历史黄金价格
    """
    try:
        spot_hist_sge_df = ak.spot_hist_sge(symbol='Au99.99')
        if days and days > 0:
            return spot_hist_sge_df.tail(days)
        return spot_hist_sge_df
    except Exception as e:
        print(f"获取历史黄金价格失败: {e}")
        return None

@app.route('/api/gold/spot_quotations_sge', methods=['GET'])
def api_realtime_gold_price():
    """
    实时黄金价格API接口
    """
    try:
        data = get_real_time_gold_price()
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
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/gold/spot_hist_sge', methods=['GET'])
def api_historical_gold_price():
    """
    历史黄金价格API接口
    支持参数: days - 获取最近N天的数据
    """
    try:
        # 获取查询参数
        days = request.args.get('days', type=int)

        data = get_historical_gold_price(days)
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
            "timestamp": datetime.now().isoformat()
        }), 500

@app.route('/api/gold/info', methods=['GET'])
def api_info():
    """
    API信息接口
    """
    return jsonify({
        "name": "黄金价格API服务",
        "version": "1.0.0",
        "description": "提供上海黄金交易所Au99.99实时和历史价格数据",
        "endpoints": {
            "/api/gold/spot_quotations_sge": "获取实时黄金价格",
            "/api/gold/spot_hist_sge": "获取历史黄金价格",
            "/api/gold/info": "API信息"
        },
        "data_source": "上海黄金交易所",
        "symbol": "Au99.99"
    })

@app.route('/', methods=['GET'])
def index():
    """
    首页
    """
    return jsonify({
        "message": "黄金价格API服务",
        "api_info": "/api/gold/info",
        "spot_quotations_sge": "/api/gold/spot_quotations_sge",
        "spot_hist_sge": "/api/gold/spot_hist_sge"
    })

if __name__ == '__main__':
    print("🚀 启动黄金价格API服务...")
    print("📊 数据来源: 上海黄金交易所 Au99.99")
    print("🌐 服务地址: http://127.0.0.1:5080")
    print("📋 API文档: http://127.0.0.1:5080/api/gold/info")
    print("-" * 50)
    app.run(debug=True, host='0.0.0.0', port=5080)