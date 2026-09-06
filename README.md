# 个人 cgit Docker 服务

[cgit](https://git.zx2c4.com/cgit/) `v1.3.1` + lighttpd，HTTP `8080`。
保留轻量仓库浏览，增加 Git Smart HTTP、Git LFS、个人账号和维护检查。
仅构建 `linux/amd64`；无需数据库、外部对象存储或运行时出站网络。

## 开始使用

```sh
docker compose up -d --build
```

把 bare repo 放入 `./repos/`，例如 `repos/demo.git` 或 `repos/group/demo.git`。
访问 `http://<ip>:8080/`，对应仓库 URL 为 `/demo` 或 `/group/demo`。

默认公开读取、禁止所有 HTTP 写入，仓库卷仍为只读；“公开”指任何能连接端口的人。
默认配置适合可信局域网/Tailscale。已有 SSH push 由宿主机自己的 Git/SSH 服务负责。

```sh
# 客户端只需安装一次 Git LFS；它负责把指针还原成真实文件。
git lfs install
git clone http://<ip>:8080/demo
git clone --depth=1 http://<ip>:8080/demo shallow-demo
```

Smart HTTP 自动处理 clone/fetch；旧 dumb HTTP 的读取端点仍然可用。
没有配置 LFS 的普通 Git 客户端只能取到指针，不能自动获得大文件。

## 个人账号与写入

没有团队角色或仓库 ACL：**一个账号管理这套服务的全部仓库**。

| 模式 | 浏览、克隆、LFS 下载 | LFS 上传 | Git HTTP push |
|---|---|---|---|
| `public`，无账号（默认） | 无需认证 | 禁止 | 禁止 |
| `public`，已配置账号 | 无需认证 | 需要认证 | 需开启且认证 |
| `private` | 全部需要认证 | 需要认证 | 需开启且认证 |

私有模式允许公共 CSS/JS/图标等静态资源，仓库页面、Git、LFS 和诊断 HTTP 入口都受认证保护。
账号使用 HTTP Basic；在不可信网络上通过 HTTPS 反向代理访问。

1. 创建 `secrets/password`，写入一行个人密码。用编辑器填写即可。
2. 确保文件仅对必要用户可读，且容器的 `PUID` 能读取它。bind mount 文件权限由宿主机控制；修改 UID/GID 时也要调整数据目录所有权。
3. 使用写入配置启动：

```sh
docker compose -f docker-compose.yml -f docker-compose.write.yml up -d --build
```

这个配置默认：账号名 `owner`、全站私有、LFS 上传和 Git HTTP push 开启、仓库卷可写。
密码不烘焙进镜像，也不写进生成的配置。`secrets/`、`.env` 和数据目录已从 Git 与构建上下文排除。
客户端按提示输入账号密码；建议使用操作系统的 Git credential helper。

新仓库需先在 `repos/` 创建 bare repo，不支持通过 HTTP 自动建库：

```sh
git init --bare --initial-branch=main repos/new-project.git
# 确保 PUID/PGID 对该目录有读写权限。
```

如果只需 LFS 上传、Git 仍走 SSH，在 `.env` 设置 `CGIT_HTTP_PUSH=0`。
若还要维持仓库挂载只读，将写入配置的 `/repos:rw` 改成 `/repos:ro`。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `PUID` / `PGID` | `1000` / `1000` | 运行用户；不会自动递归 chown Git 仓库和整个 LFS 对象库 |
| `TZ` | `Asia/Shanghai`（Compose） | 时区 |
| `CGIT_BASE_URL` | 空 | 外部 HTTP(S) origin，如 `https://git.example.com`；不支持子路径部署 |
| `CGIT_AUTH_MODE` | `public` | `public` 或 `private` |
| `CGIT_USERNAME` | 空 | 个人账号名，与密码文件一起配置 |
| `CGIT_PASSWORD_FILE` | 空 | 容器内一行密码文件路径 |
| `CGIT_HTTP_PUSH` | `0` | `1` 开启 Git HTTP push；另需账号和可写仓库卷 |
| `CGIT_LFS_MAX_SIZE` | `10737418240` | 单对象上限，字节，默认 10 GiB；同时用于请求体上限 |
| `CGIT_SSH_CLONE_URL` | 空 | 额外显示的 SSH clone URL 模板，必须包含字面量 `$CGIT_REPO_URL` |

例如 `.env`：

```dotenv
CGIT_BASE_URL=https://git.example.com
CGIT_USERNAME=owner
CGIT_AUTH_MODE=private
CGIT_SSH_CLONE_URL='git@nas:/path/to/repos/$CGIT_REPO_URL.git'
```

单引号保留 `.env` 中的 `$CGIT_REPO_URL`，由 cgit 按仓库展开。
修改环境变量后用同一组 Compose 文件重新 `up -d`。

容器继续提供普通 HTTP。TLS 放在现有反向代理上，传递原始 Host，允许 Git/LFS 的 POST/PUT 和大请求，超时按文件大小与带宽配置。
`CGIT_BASE_URL` 同时决定网页 clone URL 和 LFS action URL；不从任意 `X-Forwarded-*` 头推断地址。
不设置它时使用请求的 HTTP Host，适合直接通过 IP 访问。

`config/cgitrc` 可只读挂载到 `/etc/cgitrc`，启动时生成有效配置而不改挂载原文件。
为使 LFS 对象落盘/缺失立即体现在网页中，运行配置关闭 cgit HTML 缓存；静态资源压缩缓存保留。
使用内置 `scan-path=/repos` 和 `remove-suffix=1` 目录约定；自定义 `repo.url`/外部仓库路径不属于 Git/LFS 网关映射范围。

## LFS 使用与迁入

标准客户端从 `/demo` 自动发现 `/demo.git/info/lfs`；嵌套仓库同样支持。
上传使用 Batch + Basic transfer，流式写入，检查长度和 SHA-256 后原子落盘；下载支持 HEAD 和单段 Range。
文件锁、扩展 pointer、其他 transfer adapter、自动 GC 和远程按需拉取不在本服务范围内。

```sh
# 在本地工作仓库
git lfs track '*.psd' '*.zip'
git add .gitattributes
# 正常 add / commit / push，LFS 会在 Git push 前上传对象。
```

Git 走 SSH 时，LFS 可单独指向这里，无需改 SSH 服务：

```sh
git config remote.origin.lfsurl https://git.example.com/demo.git/info/lfs
```

已有 bare repo **可能只有指针，没有 LFS 实体**。从持有完整历史的本地克隆迁入对象：

```sh
git lfs fetch --all origin
git remote add nas https://git.example.com/demo
git lfs push --all nas
```

`origin` 是已有 LFS 来源，`nas` 是已创建对应 bare repo 的本服务。
这只迁移 LFS 对象，Git 分支/标签需要单独同步；普通 `git clone --mirror` 不能充当 LFS 备份。
如果对象只存于旧服务器本地，先用其正常支持的方式导出到客户端，再执行 `git lfs push --all nas`，不要直接套用不明存储布局。

网页 tree 页面显示 LFS 文件大小、完整下载入口及缺失状态；10 MiB 以内的常见位图内联预览，PDF 提供打开入口。
`plain/` 路径解析指针并返回真实对象；缺失返回 404，大小不符返回 422，不偷偷回源下载。

**ZIP / tar.gz 仓库归档保留 Git 中的 LFS 指针，不展开大文件。** 获取完整工作目录请使用已安装 Git LFS 的 `git clone`。

## 浏览能力

- README Markdown（代码块、表格）、语法高亮、提交图、blame、统计。
- 并排 diff 默认开启，可在页面切换；文件 log 支持跟随重命名。
- 原生提交搜索、Atom 订阅、patch 下载、分支/标签、ZIP/tar.gz 快照。
- 默认分支 README 的 Markdown 相对链接指向对应提交的 tree/raw 路径；相对图片可以加载 LFS 实体。原始 HTML 标签里的自定义相对 URL 不做重写。
- README HTML 做清理；原始 HTML/SVG 文件不作为可执行页面提供。
- 多层仓库分类、仓库元数据和 SSH clone 地址。

单仓库元数据在 **bare repo 本身** 设置：

```sh
git -C repos/demo.git config cgit.desc '我的项目'
git -C repos/demo.git config gitweb.owner 'zopiya'
git -C repos/demo.git config gitweb.category 'personal'
```

这些配置不会随普通 Git push 传输，需在服务器或挂载目录上设置。
`cgit.hide=1` 只隐藏列表项，**不是访问控制**；`cgit.ignore=1` 排除仓库，网关也拒绝其 Git/LFS 路径。
Git/LFS 网关拒绝穿越目录和符号链接仓库。仓库目录及配置属于管理员信任边界。

## 数据与维护

```text
repos/                 Git bare repositories
lfs/<repo>.git/objects/aa/bb/<sha256>
                       每仓库独立的 LFS 对象；无需数据库
cache/                 可重建配置、静态压缩缓存、上传缓冲、轮转访问日志
secrets/               个人密码文件（使用写入配置时）
```

```sh
docker exec cgit cgit-doctor
docker exec cgit cgit-doctor --git --lfs
docker logs cgit
docker exec cgit tail -n 100 /var/cache/cgit/access.log
```

doctor 输出 JSON，问题返回非零退出码。`--git` 对各仓库执行 `git fsck --full`；`--lfs` 检查所有 refs 可达的标准指针，并对存储对象计算 SHA-256，可发现缺失、损坏及中断上传残留。大对象库检查可能较慢，按需运行；不自动删除孤立对象。
root 启动的容器中 `docker exec` 默认是 root；要检查与服务相同的文件权限，可加 `--user <PUID>:<PGID>`。

`/_health` 是受读取认证保护的动态存活检查；镜像 HEALTHCHECK 仍检查静态资源，允许空仓库集合。它不代表每个仓库或 LFS 对象完整。
访问日志最多约 5 × 5 MiB；错误日志进入容器 stderr。网页缓存与大文件临时数据不放进容量有限的 `/tmp`。

### 备份与恢复

1. 暂停全部写入，包括宿主机 SSH push；停止 cgit，或对 Git/LFS 存储做一致性快照。
2. 一起备份 `repos/`、`lfs/`。密码、`.env`、自定义配置和 Compose 文件另外安全保存；`cache/` 可重建。
3. 恢复到空目录，核对 UID/GID、挂载和密码文件权限，启动后运行 `cgit-doctor --git --lfs`。
4. 用真实客户端克隆并核对关键 LFS 文件，再重新开放写入。

示例数据归档（先完成第 1 步）：

```sh
mkdir -p backups
tar -czf "backups/cgit-data-$(date +%Y%m%d-%H%M%S).tar.gz" repos lfs
```

升级可使用固定镜像 tag 回退；当前 LFS 布局为普通文件，没有自动数据迁移或破坏性清理。

## 验证与 CI

```sh
# 本地协议测试，要求 Python 3.11+、Git 和 Git LFS；不读写用户全局 Git 配置。
python3 -m unittest discover -s tests -v

# 完整镜像验证，额外覆盖真实 lighttpd、cgit 页面和过滤器。
docker build -t cgit:test .
CGIT_TEST_DRIVER=docker CGIT_TEST_IMAGE=cgit:test python3 -m unittest discover -s tests -v
```

测试只使用临时合成仓库及测试密码。无 Docker 的默认测试会明确跳过 cgit 页面检查。
CI：协议/配置测试 → amd64 构建 → 原有冒烟检查 → 容器 Git/LFS 端到端检查 → 发布 GHCR。
每周一重建，更新 Alpine 包。`latest`/`main` 为浮动 tag；`<date>-<sha>` 与版本 tag 用于追溯。

升级 cgit 时同时更新 `Dockerfile` 的 `CGIT_VERSION`、`CGIT_COMMIT`、`GIT_VERSION` 和 `GIT_SHA256`。
构建 cgit 的 Git 源码需匹配上游固定版本；运行 Smart HTTP 的 Git 二进制来自 Alpine 安全更新包，两者独立。
