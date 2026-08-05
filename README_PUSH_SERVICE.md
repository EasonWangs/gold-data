# 钉钉推送说明

服务通过钉钉自定义机器人发送上金所 `Au99.99` 行情快报。推送使用独立调度进程；Web/API 进程不会启动、停止或汇报该进程的实时存活状态，避免多 Worker 重复推送。

## 配置与启动

首次运行会生成 `dingtalk_config.json`。填入机器人 Webhook，并在运行服务的环境中设置管理令牌：

```bash
export GOLD_ADMIN_TOKEN='请替换为随机且足够长的令牌'
python gold_service.py
python gold_service.py --scheduler-only
```

`gold_service.py` 与 `--scheduler-only` 必须在同一项目目录、同一配置下运行。生产环境只允许一个调度器实例；Web/API 可由 Gunicorn 多 Worker 承载。

## 自动推送规则

| 北京时间 | 快报 | 取值与交易日语义 |
|---|---|---|
| 工作日 09:02 | 日盘开盘 | 取 09:00–09:05 的首个有效报价；归属当日交易日。 |
| 工作日 16:02 | 日线收盘 | 仅当官方当日完整日线已发布时发送；OHLC 含此前夜盘与当日日盘；若当日触发策略交叉，额外发送一条独立的 🚨 策略信号消息。 |
| 工作日 20:02 | 夜盘开盘 | 取 20:00–20:05 的首个有效报价；归属下一交易日，周五晚归属下周一。 |

源站无有效报价、不是常规交易日或完整日线尚未发布时，快报不会伪造价格。法定节假日最终以行情源的实际数据为准。

## 管理 API

所有以下接口均要求 `X-Admin-Token`。未配置 `GOLD_ADMIN_TOKEN` 返回 HTTP 503；令牌不正确返回 HTTP 401。

```bash
export GOLD_ADMIN_TOKEN='请替换为随机且足够长的令牌'

# 测试机器人连接
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://127.0.0.1:5080/api/push/test

# 手动发送开盘价、夜盘开盘价、“最新已发布日线”的模拟收盘快报，
# 或只在最新日线触发 KDJ 交叉时发送策略消息
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://127.0.0.1:5080/api/push/opening
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" 'http://127.0.0.1:5080/api/push/closing?mode=latest'
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://127.0.0.1:5080/api/push/kdj-signal
curl -X POST -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://127.0.0.1:5080/api/push/night-opening

# 查看调度规则与当前市场时段；该接口不控制调度器
curl -H "X-Admin-Token: $GOLD_ADMIN_TOKEN" http://127.0.0.1:5080/api/service/status
```

`POST /api/service/start` 和 `POST /api/service/stop` 保留为兼容路由，但始终返回 HTTP 410；请通过 systemd、supervisor 或容器编排管理独立调度进程。

## 收盘策略信号

收盘策略只以 KDJ K/D 交叉作为买入或卖出主信号。若当日触发，会额外发送独立的
`🚨 黄金策略交易信号` 消息，使用醒目的标题和“收盘确认，请重点关注”标识。消息会列出
`KDJ 买入` 或 `KDJ 卖出`、相应的 `买入权重 xN` / `卖出权重 xN`，以及触发加权的策略名称。
加权采用当前前端默认配置：MA5/20、MA10/30、MACD 方向、MACD 零轴位置各加 `1×`；KDJ
低位金叉买入额外加 `1×`、高位金叉买入减 `1×`、高位死叉卖出额外加 `1×`。没有 KDJ 交叉时不会产生策略消息。

管理页的“模拟推送最新日线收盘”和 `POST /api/push/closing?mode=latest` 使用最新已发布日线，
即使它不是今天的日线也可用于检查机器人内容；消息标题和正文会明确标注为模拟。定时器仍只在
当日官方完整日线发布后发送正式收盘快报。

管理页的“测试 KDJ 策略推送”和 `POST /api/push/kdj-signal` 会检查当前交易日：官方日线尚未
发布时，使用当日实时行情合成盘中 K 线判断；若触发 KDJ 买入或卖出，才发送同一套模拟策略消息。
盘中消息会明确标注为实时合成，正式收盘推送仍只以官方完整日线为准。未触发时返回检查结果，不会发送
任何钉钉消息或价格快报。

## 价格边界

这不是银行积存金报价。消息价格来自上金所 `Au99.99`（经 AkShare 获取）；银行积存金、纸黄金等产品可使用上金所和国际市场作为参考，但会另行加入点差、风控和交易时段规则。
