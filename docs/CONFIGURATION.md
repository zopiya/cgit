# 配置参考

[返回 README](../README.md) · [搭建指南](SETUP.md)

## Compose 文件怎么选

基础文件始终放在第一位，按需追加：

| 文件 | 用途 | 默认变化 |
|---|---|---|
| `docker-compose.yml` | Web、Git HTTP、LFS | 匿名读，Git 仓库只读，HTTP push 关闭 |
| `docker-compose.write.yml` | 单所有者 HTTP 写入 | 默认 private、owner 账号、HTTP push 开启，仓库可写 |
| `docker-compose.access.yml` | Gitolite 权限、可选 SSH | 固定 access 模式，HTTP/SSH push 默认开启，仍受 ACL 限制 |
| `docker-compose.public.yml` | Caddy HTTPS | Web 不再直接发布端口；只解决入口，不筛选数据 |
| `docker-compose.maintenance.yml` | 可选任务服务 | `maintenance` profile；检查/同步/备份 |

`write` 和 `access` 二选一或都不用。SSH 还需 `--profile ssh`，任务服务还需 `--profile maintenance`。`.env` 中显式设置的变量会覆盖文件里的默认值，例如 `CGIT_HTTP_PUSH=0` 会覆盖 write/access 的默认开启。

**旧部署调整：** maintenance 已从 access 文件拆出；此前仅用 access + maintenance profile 的命令，现在必须追加 `-f docker-compose.maintenance.yml`。固定 `container_name: cgit` 已移除，更新时容器名称可能变化，请改用 Compose 服务命令。路径和数据格式没有因此变化；不要为名称变化删除数据目录。

## 环境变量

复制根目录 [`.env.example`](../.env.example)，按需取消注释。表中默认值以基础 Compose 为准，覆盖文件的变化见上表。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `COMPOSE_PROJECT_NAME` | Compose 项目目录名 | 区分实例；示例文件设为 cgit-home |
| `PUID` / `PGID` | `1000` / `1000` | 服务 UID/GID，须与数据权限匹配 |
| `TZ` | `Asia/Shanghai` | 容器时区；任务时区另由 jobs.json 决定 |
| `CGIT_HTTP_BIND` | `0.0.0.0` | 宿主 HTTP 绑定地址 |
| `CGIT_HTTP_PORT` | `8080` | 宿主 HTTP 端口；容器内固定 8080 |
| `CGIT_BASE_URL` | 空 | 外部 HTTP(S) origin；不支持子路径；不填则使用请求 Host |
| `CGIT_AUTH_MODE` | `public` | public、private、access |
| `CGIT_USERNAME` | 空 | 单账号模式的身份名 |
| `CGIT_PASSWORD_FILE` | 空 | 容器内密码文件路径；write 文件固定为 `/run/secrets/cgit_password` |
| `CGIT_HTTP_PUSH` | `0` | Git HTTP push 总开关；另需认证/授权和可写卷 |
| `CGIT_LFS_MAX_SIZE` | `10737418240` | 单对象上限 10 GiB，也影响请求体限制；Git push 亦受入口限制 |
| `CGIT_SSH_CLONE_URL` | 空 | 网页额外展示的 SSH URL 模板；必须含字面量 `$CGIT_REPO_URL` |
| `CGIT_SSH_BIND` | `0.0.0.0` | access SSH 服务的宿主绑定地址 |
| `CGIT_SSH_PORT` | `2222` | 宿主 SSH 端口；容器内固定 2222 |
| `CGIT_SSH_PUSH` | access 文件默认 `1` | SSH receive-pack 总开关，不能绕过 ACL |
| `CGIT_DOMAIN` | 无 | public 文件必填的域名 |
| `CGIT_ACME_EMAIL` | 无 | public 文件必填的证书联系地址 |

access 文件固定 `CGIT_ACCESS_ROOT=/access`、`CGIT_GITOLITE=/usr/local/bin/gitolite`，基础文件固定 `CGIT_WRITE_ROOT=/coordination`；它们不是当前 `.env` 暴露的可变路径。改容器路径需要同步调整所有挂载、配置和 SSH 入口，不建议作为日常设置。

SSH URL 示例：

```dotenv
CGIT_SSH_CLONE_URL='ssh://git@git.example.com:2222/$CGIT_REPO_URL.git'
```

`.env` 单引号保留 `$CGIT_REPO_URL`，由 cgit 展开。该变量仅影响网页显示，不会创建 SSH 服务或改变客户端网络地址。

## 数据目录

路径相对于第一份 Compose 文件所在的项目目录；所有实例各自一套。

| 宿主目录 | 容器路径 | 内容与备份要求 |
|---|---|---|
| `repos/` | `/repos` | bare 仓库，包括本地 config、hooks、默认分支；必须备份 |
| `lfs/` | `/lfs` | 按实际仓库相对名隔离的对象；必须与 Git 配套备份 |
| `access/` | `/access` | 策略、身份哈希、公钥、快照、token key；启用时必须备份 |
| `ssh-host-keys/` | `/ssh-host-keys` | SSH 主机密钥；启用时持久化并备份 |
| `secrets/` | 按角色只读挂载 | HTTP/备份密码等；独立安全保存 |
| `cache/` | `/var/cache/cgit` | 生成配置、静态压缩缓存、临时上传、轮转日志；可重建 |
| `coordination/` | `/coordination` | public/private 写入协调锁与维护标记，不手工在线清理 |
| `jobs/` | `/jobs` | 任务声明，管理员配置；备份 |
| `job-state/` / `job-work/` | 同名根路径 | 最近任务状态 / 临时工作区 |
| `backups/` | `/backups` | 本机归档或 restic 库；同盘不算独立灾备 |
| `caddy-data/` / `caddy-config/` | `/data` / `/config` | HTTPS 状态，注意证书私钥保护 |

LFS 布局为 `lfs/group/demo.git/objects/aa/bb/<sha256>`，对应 `repos/group/demo.git`。移动或改名必须同时处理两者。

## cgit 页面配置

默认模板是 [config/cgitrc](../config/cgitrc)。可在 Compose override 中挂载自定义文件到 `/etc/cgitrc:ro`。启动时生成 `/var/cache/cgit/runtime/cgitrc` 和 `settings.json`，不改源模板。改模板后 restart，改环境变量或 Compose 后重新 up；不要直接编辑生成物。

约定 `scan-path=/repos`、`remove-suffix=1`，只支持普通目录中的 bare 仓库。自定义 `repo.*` 列表、include、非 bare 仓库、符号链接目录不属于支持范围，会明确拒绝。仓库目录/config/hooks 是管理员信任边界。

默认关闭 cgit HTML 缓存，使 LFS 状态变化及时生效；动态响应 no-store。不要自行开启而忽略身份隔离与 LFS 失效问题。

筛选与元数据见[使用指南](USAGE.md#仓库展示与发布范围)。

## 反向代理

服务部署在域名根路径，例如 `https://git.example.com`，不支持 `/git/` 子路径。代理转发至 Web 8080，保留原始 Host，允许 Git/LFS 的 POST/PUT 和大请求，设置与带宽、对象大小相符的超时和请求上限。设置 `CGIT_BASE_URL`，网关不会信任任意 `X-Forwarded-*` 来生成外部 URL。

Basic 凭据必须通过可信 HTTPS 或受控内网通道传输。现有代理启用后，从实际客户端验证页面、clone/push 和 LFS；首页能打开不能证明大文件传输可用。
