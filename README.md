# 🏅 黄金与白银价格服务

**一站式黄金、白银价格 API 和黄金钉钉推送服务**

集成数据获取、推送通知和Web管理的完整解决方案。

🌐 **服务地址**: http://127.0.0.1:5080

## 🚀 快速开始

### 安装依赖
```bash
# 在项目目录创建独立虚拟环境，避免修改系统或 Homebrew 管理的 Python
python3 -m venv .venv
source .venv/bin/activate

# 安装项目依赖
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

每次运行服务前，先执行 `source .venv/bin/activate`。请勿使用 `--break-system-packages` 绕过系统 Python 的保护。

### 启动服务
```bash
# Web/API 服务（默认仅监听本机）
# 请先执行 source .venv/bin/activate
python gold_service.py

# 定时推送必须作为单独的单实例进程运行
python gold_service.py --scheduler-only
```

### 配置钉钉推送
1. 编辑 `dingtalk_config.json` 文件
2. 替换 webhook 中的 `YOUR_ACCESS_TOKEN`
3. 设置管理令牌（请使用随机且足够长的值）：`export GOLD_ADMIN_TOKEN='请替换为随机令牌'`
4. 重启服务生效

## 🌟 核心功能

### 1️⃣ 贵金属价格API
- 🏅 **黄金**: 上海黄金交易所 Au99.99 实时与历史行情（元/克）
- 🥈 **白银**: 上海黄金交易所 Ag99.99 实时与历史行情（元/千克）
- 🧊 **源站保护**: 实时行情缓存 60 秒；历史日线及技术指标在交易时段缓存 30 分钟、其他时段缓存 12 小时
- 🔌 **标准接口**: RESTful API 设计，JSON 格式响应

### 2️⃣ 钉钉推送服务
- ⏰ **定时推送**: 工作日 09:00 开盘价 / 16:00 收盘价
- 📱 **手动推送**: 即时推送开盘价、收盘价
- 🧪 **测试功能**: 一键测试钉钉推送连接
- 🗓️ **智能排除**: 自动跳过周末

### 3️⃣ Web管理界面
- 📋 **多标签设计**: 价格数据/钉钉推送/API文档
- 🔄 **实时刷新**: 价格数据实时查看和刷新
- 🔐 **受保护操作**: 手动推送需提供管理令牌
- 🧭 **独立调度**: 定时任务由单独的单实例进程运行

## 📋 API接口文档

### 黄金与白银价格API
```bash
# 获取实时价格
curl http://localhost:5080/api/gold/spot_quotations_sge

# 获取历史价格 (最近5天)
curl http://localhost:5080/api/gold/spot_hist_sge?days=5

# 获取黄金日线 SMA（默认最近 60 个已计算交易日，MA5/10/20/30）
curl 'http://localhost:5080/api/gold/indicators/sma?days=60&windows=5,10,20,30'

# 获取黄金日线 KDJ（默认最近 60 个交易日）
curl 'http://localhost:5080/api/gold/indicators/kdj?days=60'

# 获取黄金日线 MACD（默认最近 60 个交易日）
curl 'http://localhost:5080/api/gold/indicators/macd?days=60'

# 获取API信息
curl http://localhost:5080/api/gold/info

# 获取实时白银价格
curl http://localhost:5080/api/silver/spot_quotations_sge

# 获取白银历史价格 (最近5天)
curl http://localhost:5080/api/silver/spot_hist_sge?days=5
```

### 黄金日线技术指标 API

`GET /api/gold/indicators/sma` 基于上海黄金交易所 `Au99.99` 历史日线的
`close`（元/克）计算简单移动平均线。窗口只按实际交易日滚动，不补周末或节假日；
计算会先在完整的已缓存历史序列上完成，再取最近记录，因此返回的首条记录也能拥有
足够历史时的 MA30。

| 参数 | 说明 |
|---|---|
| `days` | 返回最近多少个已计算交易日；默认 `60`，范围 `1`–`365`。 |
| `windows` | 逗号分隔的 SMA 窗口；默认 `5,10,20,30`，仅支持这四个值。 |

非法参数（包括 `days <= 0`、`days > 365`、非整数或不支持的窗口）返回 HTTP `400`，不会静默忽略。
历史交易日不足窗口时，对应 `maN` 为 `null`；`available_history_count` 给出清洗后的可用历史日线数量。

示例：

```json
{
  "status": "success",
  "timestamp": "2026-07-27T10:30:00.000+08:00",
  "symbol": "Au99.99",
  "unit": "元/克",
  "basis": "close",
  "windows": [5, 10, 20, 30],
  "data": [
    {
      "date": "2026-07-24",
      "close": 812.34,
      "ma5": 809.12,
      "ma10": 805.68,
      "ma20": 798.31,
      "ma30": 792.45
    }
  ],
  "latest": {
    "date": "2026-07-24",
    "close": 812.34,
    "ma5": 809.12,
    "ma10": 805.68,
    "ma20": 798.31,
    "ma30": 792.45
  },
  "count": 60,
  "available_history_count": 120
}
```

后续白银可沿用完全相同的响应模型扩展为 `/api/silver/indicators/sma`；EMA、布林带等也应继续归入 `indicators` 路径。

### 黄金日线 MACD API

`GET /api/gold/indicators/macd` 基于 `Au99.99` 完整历史日线的 `close`（元/克）
计算 MACD，并仅返回 `DIF` 与 `DEA`。计算使用常见的 `12,26,9` 参数：
`DIF=EMA(12)-EMA(26)`，`DEA=EMA(9, DIF)`；EMA 以首个有效收盘价初始化。
`days` 规则与 SMA 相同：默认 `60`，范围 `1`–`365`；始终先计算完整缓存历史，
再截取最近记录。

```json
{
  "status": "success",
  "symbol": "Au99.99",
  "unit": "元/克",
  "basis": "close",
  "parameters": {
    "fast_period": 12,
    "slow_period": 26,
    "signal_period": 9
  },
  "data": [
    {
      "date": "2026-07-24",
      "close": 812.34,
      "dif": 4.21,
      "dea": 3.86
    }
  ],
  "latest": {
    "date": "2026-07-24",
    "close": 812.34,
    "dif": 4.21,
    "dea": 3.86
  },
  "count": 60,
  "available_history_count": 120
}
```

### 黄金日线 KDJ API

`GET /api/gold/indicators/kdj` 基于 `Au99.99` 历史日线 `high`、`low`、`close`
计算 KDJ。`days` 的规则与 SMA 相同：默认 `60`，范围 `1`–`365`；计算始终先使用完整缓存历史，再截取最近记录。

计算口径为常见的 `9,3,3`：9 个交易日滚动 RSV，K 和 D 以 `50` 为初值，按
`K=(2×前K+RSV)/3`、`D=(2×前D+K)/3` 递推，`J=3K-2D`。不足 9 个交易日时 `k`、`d`、`j` 为 `null`；高低价区间为零时 RSV 按中性值 `50` 处理。

```json
{
  "status": "success",
  "symbol": "Au99.99",
  "unit": "元/克",
  "basis": "high-low-close",
  "parameters": {
    "rsv_window": 9,
    "k_smoothing_period": 3,
    "d_smoothing_period": 3,
    "initial_k": 50,
    "initial_d": 50
  },
  "data": [
    {
      "date": "2026-07-24",
      "close": 812.34,
      "k": 68.12,
      "d": 63.45,
      "j": 77.46
    }
  ],
  "latest": {
    "date": "2026-07-24",
    "close": 812.34,
    "k": 68.12,
    "d": 63.45,
    "j": 77.46
  },
  "count": 60,
  "available_history_count": 120
}
```

### 推送服务API
```bash
# 先在服务端设置 GOLD_ADMIN_TOKEN；所有管理接口均需此请求头
export GOLD_ADMIN_TOKEN='请替换为随机令牌'

# 测试钉钉推送
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://localhost:5080/api/push/test

# 手动推送开盘价
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://localhost:5080/api/push/opening

# 手动推送收盘价
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://localhost:5080/api/push/closing
```

`/api/service/start` 与 `/api/service/stop` 已停用，避免 Gunicorn 多 Worker 重复启动调度器。请使用进程管理器启动或停止 `python gold_service.py --scheduler-only`。

## ⚙️ 钉钉推送配置

### 创建钉钉机器人
1. 在钉钉群中点击 **群设置** → **智能群助手** → **添加机器人**
2. 选择 **自定义** 机器人
3. 设置机器人名称（如：黄金价格播报）
4. 选择安全设置（建议选择 **自定义关键词**，添加关键词如："黄金"、"价格"）
5. 复制生成的 **Webhook地址**

### 配置服务
首次运行会自动创建 `dingtalk_config.json`：
```json
{
  "webhook_url": "https://oapi.dingtalk.com/robot/send?access_token=YOUR_ACCESS_TOKEN",
  "description": "请将YOUR_ACCESS_TOKEN替换为您的钉钉机器人access_token"
}
```

编辑配置文件，将 `YOUR_ACCESS_TOKEN` 替换为实际的token。

## 🎯 使用场景

| 场景 | 功能 | 使用方式 |
|------|------|----------|
| 📱 **个人投资** | 实时监控金价变动 | Web界面查看 + 定时推送 |
| 🔌 **程序集成** | 获取金价数据 | 调用API接口 |
| 👥 **团队协作** | 统一价格信息 | 共享Web界面 + 群推送 |

## 📊 推送消息格式

### 📈 开盘价推送 (09:02)
```
📈 黄金开盘价格播报 📈

📅 日期: 2025-09-19
🏅 品种: Au99.99 (上海黄金交易所)
💰 开盘价: 825.50 元/克

⏰ 推送时间: 09:02:00
📊 数据来源: 上海黄金交易所
```

### 💼 收盘价推送 (16:02)
```
💼 黄金收盘价格播报 💼

📅 日期: 2025-09-19
🏅 品种: Au99.99 (上海黄金交易所)

💰 开盘价: 825.50 元/克
💰 收盘价: 828.20 元/克
📊 最高价: 830.00 元/克
📊 最低价: 824.00 元/克

📈 前一日收盘: 826.00 元/克
📈 涨跌额: +2.20 元/克
📈 涨跌幅: +0.27%

⏰ 推送时间: 16:02:00
📊 数据来源: 上海黄金交易所
```

## 🛠️ 技术架构

| 层次 | 技术栈 | 说明 |
|------|--------|------|
| **后端** | Flask + akshare + schedule | Web框架 + 数据获取 + 定时任务 |
| **前端** | 原生JS + CSS3 | 响应式Web界面 |
| **推送** | 钉钉机器人API | 消息推送服务 |
| **部署** | 单文件运行 | 无复杂依赖 |

## 📁 项目结构

```
📦 gold-Date/
├── 🏅 gold_service.py          # 主服务程序（唯一运行文件）
├── ⚙️ dingtalk_config.json     # 钉钉配置文件
├── 📋 requirements.txt         # 依赖包列表
└── 📖 README.md               # 项目文档
```

**核心特点**: 单文件部署，配置简单，功能完整！

## ⚠️ 注意事项

| 项目 | 说明 |
|------|------|
| 🌐 **网络要求** | 确保能访问钉钉API和上海黄金交易所 |
| ⚙️ **配置检查** | 首次使用需配置钉钉webhook地址 |
| 🔐 **管理令牌** | 必须设置 `GOLD_ADMIN_TOKEN` 才能手动或测试推送 |
| ⏰ **时区设置** | 推送时间固定使用 Asia/Shanghai（09:00/16:00）|
| 📅 **工作日历** | 自动排除周末，不包括节假日 |
| 🔌 **端口设置** | 默认端口5080，如冲突可修改 |

## 🔧 故障排除

### 常见问题
- **推送失败**: 检查webhook地址、机器人安全设置
- **数据异常**: 更新akshare库版本 `pip install --upgrade akshare`
- **端口冲突**: 修改 `gold_service.py` 中的端口号
- **依赖缺失**: 运行 `pip install -r requirements.txt`

## 🚀 部署方式

### 开发环境
```bash
export GOLD_ADMIN_TOKEN='请替换为随机令牌'
python gold_service.py                         # Web/API
python gold_service.py --scheduler-only        # 定时推送（另一个终端）
```

### 生产环境
```bash
# Web/API：可使用多个 Gunicorn Worker
pip install gunicorn
GOLD_ADMIN_TOKEN='请替换为随机令牌' gunicorn -w 4 -b 127.0.0.1:5080 gold_service:app

# 定时推送：必须只启动一个实例；建议由 systemd、supervisor 或容器编排守护
python gold_service.py --scheduler-only
```

如需对外提供 Web/API，请在反向代理后通过 HTTPS 暴露，并限制管理页面和管理接口的访问来源。

## 🎉 项目总结

这是一个**完整的、生产就绪的**黄金价格服务解决方案：

- 🎯 **功能完整**: 数据获取 + 推送通知 + Web管理
- 🚀 **部署简单**: 单文件运行，配置简单
- 💪 **部署安全**: 管理接口令牌保护，调度进程单实例运行
- 🎨 **界面友好**: 现代化Web界面，操作直观
- 🔧 **易于扩展**: 模块化设计，便于二次开发

**适合所有需要金价监控的个人和团队使用！**

---

**🏅 数据来源**: 上海黄金交易所 Au99.99 / Ag99.99
**🔧 技术栈**: Flask + akshare + 钉钉API
**📅 版本**: 2.0.0 (2025-09-19)
