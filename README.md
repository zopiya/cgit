# 轻量 cgit 一体化服务

[cgit](https://git.zx2c4.com/cgit/) `v1.3.1` + lighttpd，HTTP `8080`。
保留轻量仓库浏览，增加 Git Smart HTTP、Git LFS、个人账号和维护检查。
仅构建 `linux/amd64`；无需数据库、外部对象存储或运行时出站网络。

容器内的 Web 服务器是 **lighttpd，不是 Nginx**。请求依次经过 lighttpd、Python CGI 网关，再交给 cgit 页面、Git HTTP 后端或 LFS 文件接口。NAS/公网入口可以接已有反向代理，但不是本项目必须部署的另一项服务。

同一套代码支持个人内网匿名浏览、独立公开发布实例，以及可选 Gitolite access 协作。基础浏览不需要 access；项目自带 SSH 当前依赖 access，简单部署可使用受控宿主 SSH。公网实例只能挂载明确公开的数据，不能挂载私有库后仅靠隐藏或 ACL 保密。

当前工作区审计和未闭合风险见 [审计记录](docs/AUDIT-2026-10-31.md)，操作入口见 [维护说明](docs/OPERATIONS.md)。历史 Linux CI 与当前本地结果必须分开；当前尚未部署 NAS/VPS。本轮先审稳 Python 实现，Go 迁移文档保留为后续规划。

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

## 默认个人账号与写入

单账号 public/private 模式：**一个账号管理这套服务的全部仓库**；逐仓库 ACL 属于可选 access 模式。

需要公开/私有混合仓库、少量协作者和容器 SSH 时，使用 `docker-compose.access.yml`；该模式以 Gitolite 的 `gitolite.conf + keydir` 为权威，身份与策略应用入口是 `runtime/access.py`。历史 Linux 结果见 PROGRESS；当前工作区的验证范围和限制以审计记录为准。

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
| `CGIT_AUTH_MODE` | `public` | `public`、`private` 或可选 `access` |
| `CGIT_ACCESS_ROOT` | 空 | `access` 模式下的 `/access` 策略根目录 |
| `CGIT_GITOLITE` | 空 | `access` 模式下容器内 Gitolite 可执行文件 |
| `CGIT_USERNAME` | 空 | 个人账号名，与密码文件一起配置 |
| `CGIT_PASSWORD_FILE` | 空 | 容器内一行密码文件路径 |
| `CGIT_HTTP_PUSH` | `0` | `1` 开启 Git HTTP push；另需账号和可写仓库卷 |
| `CGIT_SSH_PUSH` | `0` | `access` SSH 模式下是否允许 `git-receive-pack` |
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
启动时检查扫描约定；不支持的 `include`、手工 `repo.*` 列表、隐藏目录扫描或非 bare 仓库会明确报错。修改挂载的 cgitrc 后需重建容器配置（重新创建容器或 restart），不要在线直接编辑生成配置。

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

- README Markdown（代码块、表格）、语法高亮、提交图、blame。
- 并排 diff 默认开启，可在页面切换；文件 log 支持跟随重命名。
- 原生提交搜索、Atom 订阅、patch 下载、分支/标签、ZIP/tar.gz 快照。
- 默认分支 README 的 Markdown 相对链接指向对应提交的 tree/raw 路径；相对图片可以加载 LFS 实体。原始 HTML 标签里的自定义相对 URL 不做重写。
- README HTML 做清理；原始 HTML/SVG 文件不作为可执行页面提供。
- 多层仓库分类、仓库元数据和 SSH clone 地址。

个人使用默认保留名称排序、按活跃时间排列分支。日志不计算每条提交的文件数/增删行数，活动统计默认关闭；commit/diff 页面仍显示详细改动。确实需要统计的仓库可单独设置 `git config cgit.max-stats month`。这些调整减少不必要的计算路径，尚无性能基准数据。

单仓库元数据在 **bare repo 本身** 设置：

```sh
git -C repos/demo.git config cgit.desc '我的项目'
git -C repos/demo.git config gitweb.owner 'zopiya'
git -C repos/demo.git config gitweb.category 'personal'
```

这些配置不会随普通 Git push 传输，需在服务器或挂载目录上设置。
`cgit.hide=1` 只隐藏列表项，**不是访问控制**；`cgit.ignore=1` 排除仓库，网关也拒绝其 Git/LFS 路径。
`cgit.ignore` 只接受 `0`/`1`，不要写 `true`/`false`；非规范值启动时报错，运行时检测到则拒绝相关请求和网页扫描，错误详情见容器日志。也支持 bare 仓库内 `cgitrc` 的 `ignore=0/1`，它按上游顺序覆盖 Git config。

`noweb` 标记、全局 `strict-export=<标记文件名>`、`project-list=<绝对路径>` 同时约束网页和 Git/LFS，后两项必须位于 `scan-path` 之前。名单按真实目录写，例如 `group/demo.git`，也可写 `group` 以包含整个分类；每行一个相对路径，空行忽略，不带末尾斜杠，不支持宏、隐藏/穿越路径或符号链接。默认不启用这些筛选，不改变现有仓库公开范围。

仓库根目录只支持普通目录和 bare 仓库；发现符号链接目录时会阻止启动或网页扫描，不让 cgit 另行跟随它。仓库配置及目录仍属于管理员信任边界。
嵌套仓库的目录分类会覆盖 `gitweb.category`；如需例外，可在该 bare 仓库的 `cgitrc` 写 `section=自定义分类`。

## 数据与维护

```text
repos/                 Git bare repositories
lfs/<repo>.git/objects/aa/bb/<sha256>
                       每仓库独立的 LFS 对象；无需数据库
cache/                 可重建配置、静态压缩缓存、上传缓冲、轮转访问日志
secrets/               个人密码文件（使用写入配置时）
access/                Gitolite 策略、身份哈希、SSH 授权和 LFS token key
ssh-host-keys/         容器 SSH 持久主机密钥
jobs/ + job-state/     maintenance 任务声明与最近状态
backups/               本地归档/restic 暂存，不替代独立灾备
```

```sh
docker exec cgit cgit-doctor
docker exec cgit cgit-doctor --git --lfs
docker logs cgit
docker exec cgit tail -n 100 /var/cache/cgit/access.log
```

doctor 输出 JSON，问题返回非零退出码。包括被 ignore/noweb/名单排除的 bare 仓库，完整性检查不随发布范围缩小；符号链接目录不跟随。`--git` 对各仓库执行 `git fsck --full`；`--lfs` 检查所有 refs 可达的标准指针，并对这些仓库的存储对象计算 SHA-256，可发现缺失、损坏及中断上传残留。大对象库检查可能较慢，按需运行；不自动删除孤立对象，也不代表已审计整个 LFS 根目录中的无主目录。
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

升级与回退记录镜像 digest；tag 可能被重建覆盖。当前 LFS 布局为普通文件，没有自动数据迁移或破坏性清理。详细步骤、备份边界及 Forgejo 迁移见 [个人维护说明](docs/OPERATIONS.md)。

## 验证与 CI

```sh
# 本地协议测试；不读写用户全局 Git 配置。报告明确标注缺少真实 Web/容器证据。
python3 scripts/check_contracts.py --stage baseline
python3 scripts/test.py --driver cgi --report artifacts/local.json

# 完整镜像验证，额外覆盖真实 lighttpd、cgit 页面和过滤器。
docker build -t cgit:test .
CGIT_TEST_IMAGE=cgit:test python3 scripts/test.py --driver docker --require-no-skips --report artifacts/container.json
```

测试要求 Python 3.11+、Git、Git LFS，只使用临时合成仓库及测试密码。CGI 驱动属于部分证据，会跳过真实 Web 检查；完整 Docker 驱动缺引擎或发生跳过即失败。

新增独立契约位于 `tests/contracts/`，从外部 HTTP/Git/LFS/配置 CLI 验证行为，不导入生产模块；原有内部回归仍保留。`contracts/runtime.json` 区分已实现基线与尚缺的 Python 完整方案/Go 交付用例，静态库存检查不等于功能验证。

CI 定义：PR/main/work 分支/定期/手动测试 → 协议基线 → amd64 构建 → 真实 cgit/lighttpd + 黑盒与现有回归。测试 job 只有读取权限，不登录 registry。仅在 main 手动选择 publish 后发布已测试的同一份镜像，并拉取 digest 复验；不会自动部署。定期构建只验证依赖更新，不自动替换正式镜像。

新工作流仍需授权推送后才能在 GitHub 上运行；本地修改不能代表远端已经启用。当前 P1/P2 的本地 native 链路通过，P3 已有任务/恢复 CLI 与 Compose/TLS 黑盒契约；Linux 镜像与 Compose/TLS 的实际执行、真实调度与 restic、Go 用例仍待后续阶段，进度见 [实施记录](docs/PROGRESS.md)。

升级 cgit 时同时更新 `Dockerfile` 的 `CGIT_VERSION`、`CGIT_COMMIT`、`GIT_VERSION` 和 `GIT_SHA256`。
构建 cgit 的 Git 源码需匹配上游固定版本；运行 Smart HTTP 的 Git 二进制来自 Alpine 安全更新包，两者独立。
