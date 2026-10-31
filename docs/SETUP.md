# 搭建指南

[返回 README](../README.md) · [配置参考](CONFIGURATION.md) · [SSH 与权限](ACCESS.md)

## 准备

当前容器目标为 Linux amd64；需要 Docker Engine 和 Compose v2 插件。使用 public 覆盖文件需要支持 `!reset` 的 Compose；CI 测试覆盖文件还需要 `!override`。请使用具备这些能力的 Compose v2，实际组合由 CI 验证，不承诺所有 v2 版本均可用。本项目未承诺支持旧版 `docker-compose` v1、ARM64 或 rootless SSH。

从本仓库目录执行命令。当前 Go 镜像未作为本轮工作发布到 registry，示例统一从源码 `--build`；不要假定远端 `latest` 已包含当前代码。生产选用经过验证、可回退的镜像 digest。

```sh
docker version
docker compose version
cp .env.example .env
id -u
id -g
mkdir -p repos lfs cache coordination
```

把 `.env` 的 `PUID` / `PGID` 改成实际服务用户。已有仓库及 LFS 对象必须允许该用户读取，写入模式还需相应写权限。启动脚本不会递归修改 Git 仓库和整个 LFS 库的所有权；新建目录也要检查权限。不要用 `chmod -R 777` 解决权限问题。

## 个人内网：匿名读取

只在可信局域网或 VPN 可达的主机上启动：

```sh
git init --bare --initial-branch=main repos/demo.git
docker compose config
docker compose up -d --build
docker compose ps
docker compose logs --tail=80 cgit
```

访问 `http://主机地址:8080/`；空仓库没有提交可浏览，写入后刷新即可。基础配置无需账号、access 策略或 SSH 容器，HTTP 写入关闭。宿主已有 SSH 等受控入口可以写入相同 bare 仓库，但其权限、备份停写由管理员负责。

若只允许本机反向代理访问，在 `.env` 设置 `CGIT_HTTP_BIND=127.0.0.1`；若从内网其他设备访问，绑定主机的内网地址并核对防火墙。端口映射不是访问控制。

## 个人 HTTP 写入

一个 HTTP 账号管理全部仓库，适合单所有者。先创建 `secrets/password`，用编辑器填入一行密码，限制为仅必要用户可读，并保证 `PUID` 能读取它。

```sh
mkdir -p secrets
# 填写 secrets/password 后：
chmod 600 secrets/password
```

在 `.env` **明确修改**：

```dotenv
CGIT_USERNAME=owner
CGIT_AUTH_MODE=private
CGIT_HTTP_PUSH=1
```

```sh
docker compose -f docker-compose.yml -f docker-compose.write.yml config
docker compose -f docker-compose.yml -f docker-compose.write.yml up -d --build
```

这会把仓库卷改为可写，网页与克隆都需 Basic 认证。若要匿名读、认证写，将 `CGIT_AUTH_MODE` 改为 `public`。若只要 LFS 上传而 Git 仍走其他入口，将 `CGIT_HTTP_PUSH` 改为 `0`；它不会关闭认证 LFS 上传。不要在不可信网络上明文传密码，先配置 HTTPS。

客户端示例见[使用指南](USAGE.md)。修改环境变量后使用同一组 `-f` 文件重新 `up -d`，不是仅 restart。

## 独立公开实例：HTTPS

为公开实例准备独立项目目录，里面只放已决定公开的 Git **全部历史**及对应 LFS 对象。它不需要 access 或个人密码。域名 A/AAAA 指向实际主机，放通所需 80/443 TCP（HTTP/3 使用 443 UDP）；只配置真正可用的地址。

`.env` 示例：

```dotenv
COMPOSE_PROJECT_NAME=cgit-public
CGIT_AUTH_MODE=public
CGIT_HTTP_PUSH=0
CGIT_DOMAIN=git.example.com
CGIT_ACME_EMAIL=admin@example.com
```

```sh
docker compose -f docker-compose.yml -f docker-compose.public.yml config
docker compose -f docker-compose.yml -f docker-compose.public.yml up -d --build
docker compose -f docker-compose.yml -f docker-compose.public.yml logs --tail=80 edge
```

访问 `https://git.example.com/` 并测试真实 clone。public 覆盖文件添加 Caddy，移除 Web 的宿主 8080 端口，并自动设置 Web 的外部 HTTPS 地址；**它不筛选仓库、不自动建立公开副本**。真实 DNS、证书签发/续期和从外网检查入口仍需现场验证。

已有 HTTPS 反向代理时不叠 public 文件：将基础 Web 绑定到受控地址，设置 `CGIT_BASE_URL=https://git.example.com`，按[代理配置](CONFIGURATION.md#反向代理)转发。

## 同一宿主运行内网和公网两套

分别使用两个完整项目目录，设置不同 `COMPOSE_PROJECT_NAME`。repos、lfs、cache、coordination、access、secrets 和主机密钥均不共用；项目名只隔离容器/网络名，不隔离 bind mount 数据。

内网实例可用 8080；公网实例由 Caddy 使用 80/443，不发布 Web 8080。如果两套都直接发布 HTTP，设置不同 `CGIT_HTTP_PORT`。SSH 端口也不能重复。当前 Compose 已取消固定容器名，统一用 `docker compose … exec cgit` 管理。

## 按需添加服务

- 容器 SSH、多用户/逐仓库权限：按 [ACCESS.md](ACCESS.md) **先初始化**，再开启 SSH。
- 定时检查、同步、备份：按 [OPERATIONS.md](OPERATIONS.md) 添加 `docker-compose.maintenance.yml`；无需启用 access。
- `write` 与 `access` 两份文件不能同时使用；public 和 maintenance 可分别叠加。

启动前用完整文件组合执行 `docker compose … config`，检查挂载、端口、认证和写入开关。以后 config/up/exec/logs/down 都沿用同一组合，避免对错实例操作。`down` 不会移除 bind mount 数据；不要对真实数据执行清理命令来排障。
