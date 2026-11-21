#!/bin/sh
# Read-only diagnostics. Deliberately omit environment variables and app logs,
# which can contain admin tokens or webhook URLs.
set -u

if ! command -v docker >/dev/null 2>&1; then
    echo "找不到 docker；请在 NAS 的 SSH 终端运行此脚本。" >&2
    exit 1
fi

if [ "$#" -eq 0 ]; then
    set -- gold-data-web gold-data-scheduler
fi

date -u '+采集时间：%Y-%m-%dT%H:%M:%SZ'
for container in "$@"; do
    printf '\n容器：%s\n' "$container"
    if ! docker inspect --type container --format '
Name={{.Name}}
Image={{.Config.Image}}
ImageID={{.Image}}
Status={{.State.Status}}
ExitCode={{.State.ExitCode}}
OOMKilled={{.State.OOMKilled}}
RestartCount={{.RestartCount}}
StartedAt={{.State.StartedAt}}
FinishedAt={{.State.FinishedAt}}
Error={{.State.Error}}
RestartPolicy={{.HostConfig.RestartPolicy.Name}}
MemoryLimitBytes={{.HostConfig.Memory}}
MemorySwapBytes={{.HostConfig.MemorySwap}}
LogConfig={{json .HostConfig.LogConfig}}
{{if .State.Health}}Health={{.State.Health.Status}}{{end}}' "$container"; then
        echo "无法读取该容器；请核对容器名和 Docker 访问权限。" >&2
        continue
    fi

    docker stats --no-stream --format \
        'Name={{.Name}} CPU={{.CPUPerc}} Memory={{.MemUsage}} MemoryPercent={{.MemPerc}} PIDs={{.PIDs}}' \
        "$container"

    # Stop at the current timestamp instead of waiting for future events.
    # Select fields explicitly, without Docker labels or environment variables.
    docker events --since 24h --until "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
        --filter type=container --filter "container=$container" \
        --filter event=oom --filter event=die --filter event=kill \
        --filter event=stop --filter event=restart --filter event=start \
        --format 'time={{.Time}} event={{.Action}} exitCode={{index .Actor.Attributes "exitCode"}} signal={{index .Actor.Attributes "signal"}}'
done

printf '\nNAS 内存（若系统支持 free）：\n'
if command -v free >/dev/null 2>&1; then
    free -m
fi

printf '\nDocker 数据目录磁盘空间：\n'
docker_root=$(docker info --format '{{.DockerRootDir}}' 2>/dev/null) || docker_root=
if [ -n "$docker_root" ]; then
    df -h "$docker_root"
else
    echo "无法读取 Docker 数据目录；请在 NAS 存储管理界面检查剩余空间。"
fi
