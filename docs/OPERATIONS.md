> 当前 Go 迁移与验证边界见 [GO-RUNTIME.md](GO-RUNTIME.md)。下文历史 Linux 结果仅对应旧 Python 提交。

# 个人维护说明

默认结构仍可用单镜像、lighttpd、Go CGI 和 cgit。工作区另有候选的 `edge + web + ssh + maintenance` Compose 角色；新增路径已通过 native 和 Linux Actions 的授权/SSH/LFS、Compose/TLS 与真实加密恢复测试，尚未在 NAS/VPS 部署。
本说明提供操作步骤，不代表已完成部署验收；不要同时启用未定义的旧 SSH 管理和容器 SSH 而不先确认端口、密钥与公网暴露范围。

## 使用边界

- HomeNAS：如果入口只在可信局域网/VPN 内，是否启用私有读取认证取决于谁能连接；默认映射 `8080:8080` 不是只监听 localhost。
- 对外分享：先确定是第三方平台账号还是自建公网入口，再安排发布范围。`access` 模式提供逐仓库 ACL，但必须同时验证网页、Git HTTP、LFS 和 SSH，不能只确认首页。
- 私有模式保护全部动态页面和 Git/LFS 读取；公开模式允许所有能连接的人读取。一个账号管理整套服务的写入。
- `CGIT_HTTP_PUSH=0` 只关闭 Git HTTP push。配置账号后仍可认证上传 LFS；是否允许 SSH push 由 `CGIT_SSH_PUSH` 和 Gitolite 规则共同决定。
- 公开仓库前检查全部分支、标签、历史及存储中的 LFS 对象。知道对象 ID 的读取者可下载该仓库存储中的对象，不要求它仍被当前分支引用。`hide` 和 robots 都不是访问控制。

## 有效配置与检查

`/etc/cgitrc` 是输入模板；启动生成 `/var/cache/cgit/runtime/cgitrc` 和 `settings.json`，后者不含密码内容。修改模板/环境变量后重新启动并核对生成结果；环境变量变化需重新创建容器。

```sh
docker exec cgit cat /var/cache/cgit/runtime/cgitrc
docker exec --user <PUID>:<PGID> cgit cgit-doctor
docker exec --user <PUID>:<PGID> cgit cgit-doctor --git --lfs
```

替换尖括号中的实际 UID/GID；按服务用户检查，避免 root 掩盖权限问题。完整扫描包含未发布的普通 bare 仓库，不跟随符号链接。Git fsck 证明对象结构，不证明某次误删的分支仍然存在；LFS 检查只覆盖标准指针和已发现仓库的对象目录。

`cgit.ignore` 和 bare 仓库内 cgitrc 的 `ignore` 只使用 `0`/`1`。如果旧配置写了 `true`，由管理员明确改为 `1` 后再启动；程序不自动修改仓库。网页请求会重新检查扫描目录和这个标志，避免运行期间改错配置后绕过检查；这会增加一次目录遍历和每仓库一次 Git config 读取，适合当前个人仓库规模，不宣称性能收益。

页面缓存保持关闭，包括固定提交的页面：LFS 文件可能在 Git 提交不变时迁入或丢失。动态页面 no-store，静态资源压缩缓存保留。不要仅改模板 cache-size 就认定缓存开启。

保留现有大文件上限与读写空闲超时；请求体上限同时影响 Git push。若出现失败，先检查客户端、入口代理、lighttpd 和磁盘容量，再根据具体文件与带宽调整。访问日志有轮转，Docker stderr 日志是否轮转还需核对宿主机设置。

## access 网页登录入口

access 模式生成的 cgit 页面包含“登录 / Sign in”链接。链接访问顶层 `/_login`：匿名请求收到 Basic 认证挑战，有效的 HTTP 身份收到固定指向 `/` 的 303 跳转；LFS Bearer 凭据不能用于网页登录。借助顶层挑战建立根目录的 Basic 认证范围，避免先知道某个私有仓库地址才能登录。

这沿用浏览器 Basic 弹窗，没有新增 cookie 会话、注册、修改密码或退出登录功能；浏览器对凭据缓存和切换账号的行为仍需浏览器层验收。SSH 公钥与 HTTP 凭据关联同一个身份，但不能互相替代。

## access、SSH 与 maintenance 候选入口

以下入口已在隔离 Linux CI 验证；真实部署仍需按进度记录中的人工项验收：

```sh
# Web + Gitolite ACL；SSH profile 需显式启用。
docker compose -f docker-compose.yml -f docker-compose.access.yml --profile ssh up -d --build

# 公网 HTTPS；Web 后端不再发布为宿主 8080。
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.public.yml --profile ssh up -d
```

`access/` 是 Gitolite 策略、编译快照、身份哈希、authorized_keys 和 LFS token key 的共享根。修改人员、公钥或公开范围必须走同一 apply 流程：进入 maintenance、编译、生成快照、再次查询验证，最后退出 maintenance。不要手工改 `identity.json` 或只替换某个挂载文件来绕过 generation 校验。

SSH 角色使用专用健康检查，不能继承共享 Web 镜像的 HTTP 检查。OpenSSH 的认证隔离需要 `SYS_CHROOT`；其余能力仍按 Compose 的显式列表授予。新建服务账户使用 `/bin/sh` 执行已登记密钥的 forced command，并解除 Alpine 默认账户锁定；密码和交互登录继续由 sshd/forced command 拒绝。使用现有 UID 时，应核对对应账户的 shell 与锁定状态；入口不擅自解锁已有账户。

SSH 只允许 Git upload/receive pack 和 `git-lfs-authenticate`；不支持交互 shell、scp/sftp、转发或管理命令。SSH 签发的是短期 HTTPS LFS token，LFS 数据仍走 Web，不是纯 SSH LFS transport。

`jobs.example.json` 可复制为 `jobs/jobs.json`。任务服务使用独立镜像、独立状态目录和 `/job-work`；`check` 默认启用，archive/restic backup 示例默认关闭。先准备 `secrets/restic_password` 和明确备份目标，再改配置启用。

`sync-in` 的来源只接受绝对本地路径、`https://` 或 `ssh://` URL。URL 不允许内嵌凭证、query、fragment 或 SSH 密码，远端 `.lfsconfig` 被拒绝；Git 子进程仅允许 `file/https/ssh`，不会执行 `ext::` 等 remote helper。远端删除和非快进改写默认让任务失败并保留旧 refs。

```sh
# 启停任务 profile；实际调度与跨容器写栅栏仍需 Linux 验收。
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  --profile maintenance up -d

# 同一配置手动运行任务并查看最近状态。
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  --profile maintenance run --rm --entrypoint /usr/local/bin/cgit-runtime maintenance \
  jobs run --id integrity-check
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  --profile maintenance run --rm --entrypoint /usr/local/bin/cgit-runtime maintenance \
  jobs status
```

restic 恢复必须指向不存在的空目标。命令先让 restic 恢复，再校验 access generation、编译策略、仓库 `gl-conf`、SSH 授权、LFS token key，并运行完整 Git/LFS 完整性扫描；失败目录会保留供排查，不会自动覆盖或删除。

```sh
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  --profile maintenance run --rm --entrypoint /usr/local/bin/cgit-runtime maintenance \
  jobs restore \
  --repository /backups/restic --password-file /secrets/restic_password \
  --snapshot latest --destination /backups/restore-drill
```

合成 restic 的 CLI 契约通过不等于真实加密仓库、凭据轮换、独立故障域或客户端 clone 已验收。正式恢复仍需独立容器、只读入口、真实 Git/LFS 客户端和权限矩阵。

## 托管、同步和备份

| 副本 | 用途 | 能否恢复误删 |
|---|---|---|
| NAS 托管仓库 | 日常 clone/push | 不保证 |
| Git mirror/同步副本 | 跟随分支、标签变化 | 会同步误删或强推，单靠它不保证 |
| 有保留历史的备份 | 恢复某个时间点的数据 | 需同时保存 Git/LFS，并完成恢复验证 |

备份流程：

1. 暂停 HTTP、SSH、同步任务等所有写入来源；单独停止 cgit 不会阻止 SSH push。
2. 对 `repos/` 和 `lfs/` 做同一时间窗口的一致性快照或归档。等待正在上传的 LFS 文件完成；不清理不明残留。
3. 另外安全保存 Compose、自定义 cgitrc、`.env`、密码文件、UID/GID 和当前镜像 digest；秘密文件按现有安全方式保存。
4. `cache/` 无需备份；它含可重建配置、压缩缓存、缓冲和日志。
5. 把完成的备份复制到独立设备/位置并保留历史版本。NAS 同盘上的 `backups/` 只是临时归档，不能抵御整机或整盘故障。

恢复演练：

1. 将选定备份恢复到独立空目录，绝不覆盖正在使用的目录。
2. 使用记录的镜像 digest、配置与 UID/GID，先校对 Git/LFS 对应路径和权限。
3. 建立隔离的测试容器与独立 cache；仅绑定本机回环端口，关闭 HTTP push，不接 SSH 或同步写入。
4. 以服务 UID 执行 `cgit-doctor --git --lfs`，确认仓库清单符合备份记录。
5. 实际 clone、切换关键分支/标签，核对 LFS 文件内容；仅看到首页或 Git fsck 成功不算恢复完成。
6. 保存演练日期、所用备份和结果。通过后再单独安排正式恢复与重新开放写入。

## Forgejo 迁移

先保留旧服务和备份，在单个测试仓库验证迁移，再扩到其他仓库。

- 同步 Git 分支、标签和必要 refs；确认 bare 仓库的 HEAD 指向存在的默认分支。不要直接对已有目标执行可能删除 refs 的 mirror push。
- `git clone --mirror` 不会下载 LFS 实体。通过旧服务支持的客户端方式 `git lfs fetch --all`，再向新服务 `git lfs push --all`；迁移完成前别删除旧对象。
- 仓库本地 config、description 和 hooks 不随普通 Git push 迁移。逐项重建需要的元数据，检查并停用依赖 Forgejo 的旧 hooks，别直接执行旧服务脚本。
- Issues、PR、附件、Releases、账号和权限不会转成 cgit 功能；如需保留，单独导出。
- 移动/改名 `repos/group/demo.git` 时，配套迁移 `lfs/group/demo.git`，并更新客户端地址；只改 Git 目录会导致 LFS 看似丢失。
- 迁移验收包括完整性检查、真实客户端 clone/fetch/push、LFS 内容和读取认证；确认前保留回退来源。

## 升级与回退

1. 记录当前容器 image ID、可拉取的仓库 digest、配置和数据备份。`latest`、版本 tag、日期-SHA tag 都不能替代 digest；当前日期取自提交日期，同一提交的定期重建可以覆盖同名 tag。
2. 在隔离测试目录验证候选镜像的启动、权限、网页、Git/LFS 和完整性检查。不使用正式目录测试写入。
3. 获得实际部署授权后再切换正式服务；保留原 digest 与对应配置，避免同时改版本、权限和存储布局。
4. 出现问题优先停止写入、检查失败层，再切回旧镜像与兼容配置。镜像回退不会撤销数据写入；需要回退数据时，从一致性备份单独恢复。

当前没有自动数据迁移或 LFS GC，维护命令不会主动删除对象。

## 三种部署与本轮审计边界

当前工作区问题、修复证据及试点条件见 [审计记录](AUDIT-2026-10-31.md)。两个独立实例使用不同项目目录中的 repos、lfs、cache、secrets 和 SSH 主机密钥；同一宿主还需覆盖固定 container_name 与发布端口。不要用同一个私有数据目录同时启动公网实例。

- 个人内网匿名读取：基础 docker-compose.yml；写入由受控宿主入口负责，不需要 access 身份文件。
- 独立公开发布：基础 + docker-compose.public.yml，设置 CGIT_DOMAIN/CGIT_ACME_EMAIL；仅放公开副本，不必叠 access。
- 可选协作：基础 + access，按需启用 ssh/maintenance profile；不要再叠 write 覆盖文件。

人员与公钥入口：access/.gitolite/conf/gitolite.conf、access/keydir 和管理员 identities JSON，凭据哈希由 `cgit-runtime access hash-secret --secret-file <临时凭据文件>` 生成；`cgit-runtime access apply --directory <access绝对路径> --repos <repos绝对路径> --identities <身份文件> --gitolite <gitolite绝对路径>` 编译并发布快照/authorized_keys。owner 标记不替代 Gitolite R/RW 规则。身份禁用、公钥撤销需重新 apply，不能只编辑快照。初始化与失败重试应先在合成环境演练，不在运行中的数据目录试错。

新库由管理员 `git init --bare --initial-branch=main <repos/name.git>` 创建；access 模式还必须更新 Gitolite 策略并 apply。仓库重命名必须一并迁移对应 lfs/<逻辑仓库名>，重新应用策略并校验旧/新 URL；目前没有原子改名 CLI。

任务配置入口为 jobs/jobs.json（从根目录 jobs.example.json 复制）；`cgit-runtime jobs --help` 列出 validate/crontab/status/run/restore 等实际命令。archive 仅保存 repos/lfs，不能代替完整服务恢复。restic 默认数据为 repos/lfs/access；宿主秘密、源配置、SSH 主机密钥是否覆盖取决于显式 paths，必须核对，而不是假设默认已保存。恢复到独立空目录后再做完整 Git/LFS/权限与客户端检查，不覆盖运行实例。

当前 restore 验收要求 access 完整快照，简单无 access 部署尚无对应验收模式。Go 项目内 HTTP/LFS/SSH 写入与维护共用协调锁，apply 等待在途写入排空；宿主 SSH 等外部入口仍须先停写。跨容器运行效果仍待 Linux 验收。失败后的 maintenance 标记需确认没有存活任务/写入、核对策略和备份完整性后处理，不要自动删除标记恢复服务。

备份目的地与原始数据同盘只能防部分误操作；独立故障域、保留周期及离线恢复密码需由实际部署配置落实。本轮没有替用户建立异地备份或保管恢复密钥。
