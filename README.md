# 黄金价格API服务

使用 akshare 库提供黄金实时和历史价格数据的 REST API 服务。

## 安装依赖

```bash
pip install -r requirements.txt
```

或者单独安装：

```bash
pip install akshare pandas requests flask
```

## 启动API服务

```bash
python gold_price_api.py
```

服务将在 http://127.0.0.1:5080 启动

## API接口文档

### 1. 获取实时黄金价格
```
GET /api/gold/spot_quotations_sge
```

**响应示例：**
```json
{
  "status": "success",
  "timestamp": "2025-09-18T17:07:35.000",
  "data": [
    {
      "品种": "Au99.99",
      "最新价": 825.50,
      "涨跌": "+2.30",
      "涨跌幅": "+0.28%"
    }
  ],
  "count": 542
}
```

### 2. 获取历史黄金价格
```
GET /api/gold/spot_hist_sge?days=N
```

**参数：**
- `days` (可选): 获取最近N天的数据，不传则返回所有历史数据

**响应示例：**
```json
{
  "status": "success",
  "timestamp": "2025-09-18T17:07:39.000",
  "data": [
    {
      "date": "2025-09-18T00:00:00.000",
      "open": 832.8,
      "close": 824.59,
      "high": 837.0,
      "low": 822.0
    }
  ],
  "count": 5,
  "days": 5
}
```

### 3. 获取API信息
```
GET /api/gold/info
```

### 4. 首页
```
GET /
```

## 测试API

运行测试客户端：
```bash
python api_test.py
```

## 功能特性

- 🚀 REST API 服务
- 📊 实时黄金价格数据 (spot_quotations_sge)
- 📈 历史价格数据 (spot_hist_sge)
- 🔍 支持天数筛选
- 📋 标准JSON响应格式
- ⚡ 基于Flask框架

## 数据源

- **上海黄金交易所 Au99.99**
- 通过 akshare 库获取数据
- 实时更新

## 错误处理

API 会返回标准的错误响应：
```json
{
  "status": "error",
  "message": "错误信息",
  "timestamp": "2025-09-18T17:07:35.000"
}
```

## 注意事项

1. 确保网络连接正常
2. akshare 库需要访问上海黄金交易所数据
3. 生产环境建议使用 WSGI 服务器如 Gunicorn
4. 可以通过修改端口来避免冲突