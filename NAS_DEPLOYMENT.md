# NAS Docker 部署

本项目使用两个容器：`web` 提供网页和 API，`scheduler` 是唯一的定时推送进程。请不要额外启动第二个 `scheduler` 容器，否则同一时点可能重复推送。

## 1. 准备配置

在项目目录执行：

```bash
cp .env.example .env
```

编辑 `.env`，将 `GOLD_ADMIN_TOKEN` 改为高强度随机值。启动服务后，打开后台「推送管理」，输入管理令牌并在「推送渠道配置」中填写机器人 Webhook、飞书签名密钥（如有）和实际可访问的 `link_url`。

机器人密钥由 Compose 的 `push-runtime` 数据卷持久化，页面不会回显，也不会进入镜像或版本库。

## 2. 构建并启动

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f web
```

默认通过 `http://NAS_IP:5080` 访问；健康检查地址为 `http://NAS_IP:5080/api/info`。群晖 Container Manager 可在“项目”中选择本目录的 `compose.yaml` 创建项目，或通过 SSH 运行以上命令。

## 3. 反向代理与 HTTPS（推荐）

若由群晖反向代理、Nginx Proxy Manager 或其他网关提供域名和 HTTPS，将 `.env` 中的端口设为：

```env
GOLD_HOST_PORT=127.0.0.1:5080
```

然后把反向代理的上游设为 `http://127.0.0.1:5080`，并在后台将已启用渠道的管理界面链接改为 HTTPS 域名。若反向代理运行在另一个 Docker 容器中，不要使用 `127.0.0.1`；应把两个服务加入同一 Docker 网络，并以服务名访问。

若前端必须从不同域名直接访问 API，在 NAS 的 `.env` 中配置前端页面的实际来源（不是 API 地址）：

```env
GOLD_CORS_ALLOWED_ORIGINS=https://frontend.example.com
```

多个前端来源用英文逗号分隔。服务只会向明确列出的 `http` 或 `https` 来源返回 CORS 响应头；请勿设置通配符 `*`。修改后执行 `docker compose up -d --build` 使配置生效。

## 4. 更新与排查

```bash
# 更新代码后重新构建并重建容器（Web 会有短暂中断）
docker compose up -d --build

# 查看两个容器的日志
docker compose logs -f web scheduler

# 停止并删除容器（保留推送配置数据卷）
docker compose down
```

管理接口继续使用请求头 `X-Admin-Token`，其值为 `.env` 中的 `GOLD_ADMIN_TOKEN`。Compose 的 `scheduler` 只有一个副本；不要通过横向扩容该服务。

## 5. 容器停止或反复重启时

**先采集退出信息，再重建容器**。重建会丢失旧容器的退出状态和重启计数。在 NAS 的 SSH 终端、项目目录内执行：

```bash
sh scripts/diagnose-nas.sh

# 若在 NAS 界面中自定义了容器名，把实际名称作为参数传入
sh scripts/diagnose-nas.sh your-web-container your-scheduler-container

# 本地查看异常发生前后的应用日志；分享前遮盖令牌、Webhook 等敏感值
docker compose logs --timestamps --tail=200 web scheduler
```

脚本只读取退出码、OOM 标记、重启策略、内存限制、资源占用、Docker 事件和磁盘空间；不输出环境变量或应用日志。Docker 只保留有限的近期事件，NAS/Docker 重启或容器重建后可能无法查到旧故障。`docker stats` 是采集时的快照，不代表故障时的内存峰值。

| 证据 | 判断与处理 |
| --- | --- |
| `OOMKilled=true` 或 `oom` 事件 | 存在内存不足杀进程记录。结合 NAS 总内存、其他容器占用和本容器限制排查；调整过小的容器内存上限或腾出主机内存。不要直接设置更小的 `mem_limit`。 |
| `ExitCode=137`，但没有 OOM 证据 | 进程收到了 `SIGKILL`；还可能是停止超时或外部强制终止，不能只凭 137 判定内存不足。结合 `kill` 事件与 NAS 系统日志检查。 |
| `ExitCode=1` 或 `State.Error` 有内容 | 对照应用末尾日志检查依赖导入失败、配置错误、卷权限和端口冲突等启动问题。 |
| `ExitCode=143`、`stop`/`kill` 事件 | 通常是 `SIGTERM` 停止，检查 NAS 定时任务、自动更新、休眠/关机和人工操作。 |
| `RestartPolicy=no` | NAS 界面手工建容器没有继承 Compose 配置。启用异常退出后自动重启，或用本项目 Compose 重建。 |
| 容器运行但 `unhealthy` | 属于健康检查失败，需要进一步排查请求阻塞；普通 Docker Compose 不会仅因 `unhealthy` 自动重启容器。 |

仓库的 `unless-stopped` 策略用于退出后的自动恢复；手动停止的容器不会自行恢复。退出码与事件需一起判断，详见 [Docker 重启策略](https://docs.docker.com/engine/containers/start-containers-automatically/)。

## 6. 已配置的稳定性措施与边界

- 镜像默认通过 Waitress 监听 `0.0.0.0:5080`，Compose 使用同一个默认命令。在 NAS 界面直接使用镜像时，请清除旧的 `python gold_service.py` 自定义启动命令；镜像里的 `CMD` 不会覆盖界面中保存的命令。
- Web 使用 4 个工作线程、最多 64 个活动连接、64 个待接入连接，空闲连接超时 60 秒；请求体和输出缓冲高水位均为 1 MiB。输出缓冲高水位用于背压，不限制历史行情响应的总大小。这些设置约束部分资源开销，不是整个进程的内存上限。
- Compose 为两个服务启用 `init`、30 秒停止宽限期，并将每个容器日志轮转为最多 3 份、每份 10 MiB，避免日志长期无限增长。轮转不会立即清理旧容器日志，需重建容器生效，参见 [Docker 日志配置](https://docs.docker.com/engine/logging/drivers/json-file/)。
- 镜像启用 Python `faulthandler`，部分原生崩溃会把 Python 栈写入容器日志；OOM 或 `SIGKILL` 不能靠 Python 捕获。
- `init` 与停止宽限期不等于业务任务已实现优雅退出；更新时尽量避开推送窗口。

**行情请求与缓存保护**：`sge_client.py` 同时支持直连上金所和通过 AkShare 获取实时分时、历史日线。两种方式的连接超时均为 3.05 秒，读取空闲超时为 10 秒；超时和解析失败会走现有失败响应，不自动重试、不把失败当成有效行情缓存。直连方式会在解析前检查 HTTP 错误；AkShare 方式使用 SDK 自身的请求及解析逻辑，并通过其原生 `set_timeout` 配置超时，不修改全局 Requests。连接/读取超时不是整个请求的硬截止时间（DNS 解析、多 IP 连接尝试、持续慢速传输等可能使总时长更长），参见 [Requests 超时说明](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts)。Waitress 的 `channel_timeout` 只针对客户端空闲连接，不能代替上游请求超时。

缓存的全局锁只保护内存状态；网络请求和指标计算都在锁外执行。同一行情类型、品种的并发缓存刷新共享一次抓取及其结果/异常，不同缓存键互不等待。等待同一刷新最多 20 秒，等待超时不会取消原请求或额外发起重复抓取；这是等待请求的限制，不是后台任务的强制终止机制。有效缓存仍可直接读取，强制刷新跳过已有缓存，但会合并正在进行的同键刷新。失败不会覆盖仍有效的旧缓存，也不会把过期数据当作新数据返回。

历史行情刷新与指标计算并发时，通过历史数据快照身份校验，避免旧计算结果重新写入已失效的指标缓存。刷新之前已经开始的请求可完成并返回它使用的旧快照，新快照的计算不必等待它。测试覆盖实际本地 HTTP 读取超时、失败恢复、同键合并、不同键并发、缓存过期和指标失效竞态：

```bash
source .venv/bin/activate
python -m unittest discover -s tests -v
```

测试不会访问真实行情源或发送推送；其中一个测试临时监听 `127.0.0.1` 的随机端口以模拟无响应源站。这些应用层修复能降低阻塞风险，但仍需 NAS 的退出记录才能确认容器停止的根因。

AkShare 已固定为 `1.20.65`，避免重新构建时因接口改名漂移。其他依赖仍使用版本下限，每次重新构建可能安装不同版本。生产环境宜保留已验证的镜像版本以便回滚；避免通过定时重启或盲目增大线程数掩盖故障。

## 7. 切换行情获取方式

在 NAS 项目的 `.env` 中设置 `GOLD_SGE_PROVIDER`：

| 值 | 获取方式 |
| --- | --- |
| `direct`（默认） | Requests 直接访问上金所；启动时不导入 AkShare。 |
| `akshare` | 使用固定版本 AkShare 的 `spot_realtime_sge` 与 `spot_hist_sge`，由 SDK 获取并解析同一上金所数据源。 |

例如切换到 AkShare：

```env
GOLD_SGE_PROVIDER=akshare
```

使用本仓库源码构建时，更新代码并重建两个服务：

```bash
docker compose up -d --build --force-recreate web scheduler
curl http://127.0.0.1:5080/api/info
```

若在 NAS 界面直接创建容器，先下载包含此功能的新镜像，在 Web 和 scheduler 两个容器的环境变量中同时设置该值，再重建容器。单独执行 `docker restart` 不会应用修改后的环境变量。切回直连时将值改为 `direct` 并同样重建。

`/api/info` 的 `data_provider` 应显示所选值，并列出 `supported_data_providers`；实时和历史成功响应也包含 `data_provider`。如果缺少这些字段，说明当前访问的仍是旧版后端或代理目标没有更新。每个进程在启动时固定获取方式，切换后使用新缓存；没有按请求查询参数切换或失败后自动切换。

AkShare 模式要求安装 `requirements.txt` 指定的依赖（Python 至少 3.11.5，镜像使用 3.12）。不兼容的旧 SDK 或错误配置会在启动时明确报错，不退回到无超时请求。若旧版日志显示 `spot_quotations_sge` 属性不存在，是旧调用名称与新版 AkShare 不兼容；本版本已使用新版实时接口名称。
