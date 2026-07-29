# NAS Docker 部署

本项目使用两个容器：`web` 提供网页和 API，`scheduler` 是唯一的钉钉定时推送进程。请不要额外启动第二个 `scheduler` 容器，否则同一时点可能重复推送。

## 1. 准备配置

在项目目录执行：

```bash
cp .env.example .env
cp dingtalk_config.example.json dingtalk_config.json
```

编辑 `.env`，将 `GOLD_ADMIN_TOKEN` 改为高强度随机值；编辑 `dingtalk_config.json`，填入机器人 Webhook，并将 `link_url` 改为用户可以访问的实际域名或 NAS 地址。

这两个文件均不应提交至版本库。已有可用的 `dingtalk_config.json` 时，直接复制它到 NAS 项目目录即可。

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

然后把反向代理的上游设为 `http://127.0.0.1:5080`，并将 `dingtalk_config.json` 的 `link_url` 改为 HTTPS 域名。若反向代理运行在另一个 Docker 容器中，不要使用 `127.0.0.1`；应把两个服务加入同一 Docker 网络，并以服务名访问。

## 4. 更新与排查

```bash
# 更新代码后重新构建并滚动重建
docker compose up -d --build

# 查看两个容器的日志
docker compose logs -f web scheduler

# 停止并删除容器（保留本地配置文件）
docker compose down
```

管理接口继续使用请求头 `X-Admin-Token`，其值为 `.env` 中的 `GOLD_ADMIN_TOKEN`。Compose 的 `scheduler` 只有一个副本；不要通过横向扩容该服务。
