# 🏅 黄金价格服务

**一站式黄金价格API和钉钉推送服务**

集成数据获取、推送通知和Web管理的完整解决方案。

🌐 **服务地址**: http://127.0.0.1:5080

## 🚀 快速开始

### 安装依赖
```bash
pip install akshare pandas requests flask schedule
# 或使用
pip install -r requirements.txt
```

### 启动服务
```bash
python gold_service.py
```

### 配置钉钉推送
1. 编辑 `dingtalk_config.json` 文件
2. 替换 webhook 中的 `YOUR_ACCESS_TOKEN`
3. 重启服务生效

## 🌟 核心功能

### 1️⃣ 黄金价格API
- 📊 **实时价格**: 上海黄金交易所 Au99.99 实时行情
- 📈 **历史数据**: 支持天数筛选的历史价格查询
- 🔌 **标准接口**: RESTful API 设计，JSON 格式响应

### 2️⃣ 钉钉推送服务
- ⏰ **定时推送**: 工作日 09:02 开盘价 / 16:02 收盘价
- 📱 **手动推送**: 即时推送开盘价、收盘价
- 🧪 **测试功能**: 一键测试钉钉推送连接
- 🗓️ **智能排除**: 自动跳过周末

### 3️⃣ Web管理界面
- 📋 **多标签设计**: 价格数据/钉钉推送/API文档
- 🔄 **实时刷新**: 价格数据实时查看和刷新
- 🎛️ **服务管理**: 一键启停推送服务
- 📊 **状态监控**: 服务运行状态和推送统计

## 📋 API接口文档

### 黄金价格API
```bash
# 获取实时价格
curl http://localhost:5080/api/gold/spot_quotations_sge

# 获取历史价格 (最近5天)
curl http://localhost:5080/api/gold/spot_hist_sge?days=5

# 获取API信息
curl http://localhost:5080/api/gold/info
```

### 推送服务API
```bash
# 启动推送服务
curl -X POST http://localhost:5080/api/service/start

# 停止推送服务
curl -X POST http://localhost:5080/api/service/stop

# 获取服务状态
curl http://localhost:5080/api/service/status

# 测试钉钉推送
curl -X POST http://localhost:5080/api/push/test

# 手动推送开盘价
curl -X POST http://localhost:5080/api/push/opening

# 手动推送收盘价
curl -X POST http://localhost:5080/api/push/closing
```

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
| ⏰ **时区设置** | 推送时间基于本地时区（09:02/16:02）|
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
python gold_service.py
```

### 生产环境
```bash
# 方式1: 后台运行
nohup python gold_service.py > gold_service.log 2>&1 &

# 方式2: 使用 gunicorn
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:5080 gold_service:app
```

## 🎉 项目总结

这是一个**完整的、生产就绪的**黄金价格服务解决方案：

- 🎯 **功能完整**: 数据获取 + 推送通知 + Web管理
- 🚀 **部署简单**: 单文件运行，配置简单
- 💪 **性能稳定**: 多线程处理，错误恢复
- 🎨 **界面友好**: 现代化Web界面，操作直观
- 🔧 **易于扩展**: 模块化设计，便于二次开发

**适合所有需要金价监控的个人和团队使用！**

---

**🏅 数据来源**: 上海黄金交易所 Au99.99
**🔧 技术栈**: Flask + akshare + 钉钉API
**📅 版本**: 2.0.0 (2025-09-19)