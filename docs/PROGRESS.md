# 实施进度与证据

目标：完整实现 [总体方案](PLAN.md)、[安全要求](SECURITY-DESIGN.md) 与 [Python→Go/CI 路线](RUNTIME-MIGRATION-AND-CI.md)。不是以当前通过的测试缩小完成范围。

## 2026-10-31：P1 测试契约与 CI 基础（进行中）

已写入工作区：

- `tests/contracts/`：独立 HTTP/Git/LFS 黑盒契约；`tests/contract_support.py` 仅通过 CLI、环境、卷和客户端配置夹具，不导入生产模块。
- `runtime/configure.py` 增加 CLI 路径参数，默认容器启动调用保持原值；本地夹具现在也实际执行配置生成器。
- `contracts/runtime.json`：基线与未来 Python/Go 验收项分层。完整方案尚无用例的项目保留为未实现，不能用库存检查冒充覆盖。
- `scripts/test.py`：CGI/native/Docker 分层结果，保存 JSON；Docker 预检、关键 skip/expected failure 拒绝机制。报告永不宣称 production_verified。
- Actions 工作流：增加 PR/work 分支检查，测试只读；只在 main 手动授权发布同一份经过验证的镜像。Actions 依赖固定到官方标签对应提交，发布与测试权限隔离。

已取得的本轮证据：

- 修改前基线：31 项，2 项真实 Web 专属检查跳过；未发现协议回归。
- 新黑盒契约 CGI：8 项通过，真实 Web 类明确跳过；真实 Git/Git LFS 客户端往返通过。
- 新旧合并 CGI：39 项执行成功，报告含 3 个 skip 记录（其中包含一个整类 skip），是协议部分证据，不能按简单减法推算用例总数。
- `check_contracts --stage baseline` 库存检查通过；`--stage python` 对缺少 ACL/SSH/任务等用例正确返回失败。
- compileall、ShellCheck、diff 格式检查通过；最终工作流通过 actionlint，且已静态检查只读测试与手动 main 发布的权限边界。
- 临时目录已从官方源码构建 cgit v1.3.1（固定提交）及 lighttpd 1.4.82，准备完整 native 集成。Git 源码 SHA-256 与 Dockerfile 固定值一致；cgit 本地验证禁用 Lua，并仅在临时构建添加 macOS memrchr 兼容定义，不改变正式镜像源码。

- **最终真实 native：42 项全部通过，无 skip、无 expected failure。** 覆盖新旧协议、网页/高亮/归档/gzip、私有认证、排除/项目名单/标记文件、客户端 Git/LFS 往返与完整性检查。报告为本地忽略目录 `artifacts/p1-native.json`。
- 真实服务发现并修正两处基线问题：cgit 旧 `?r=` 入口对排除库原生返回 400（其他形式 404），已按精确入口断言且确认无内容泄漏；project-list 分类路径的末尾斜杠导致 URL 映射不一致，改为规范路径并补先失败后通过的回归。
- Docker 缺失预检实际返回失败，报告不会把它转成绿色 skip；Python-complete 阶段库存门槛实际拒绝尚未实现的完整目标。

尚未取得的证据：Linux 镜像/Compose 和 GitHub Actions。临时 native 服务不能代替 Alpine 容器。新增空库镜像冒烟脚本验证安全参数下的入口启动、静态资源、动态健康和镜像 HEALTHCHECK；因无 Docker 尚未运行。

本机无 Docker；GitHub CLI 当前认证不可用，但已连接的 GitHub 插件可识别用户。没有据此自动提交/推送、创建远端测试分支、触发 Actions 或发布。远端测试需要先明确获准推送测试分支；NAS/VPS 部署不属于这一步。

## 后续阶段预备探测（不是 P2 完成）

等待远端 CI 授权期间，在临时目录用 Gitolite v3.6.14（提交 b427f928615f60d3b361472748d8a768d493bda0）做原生 CLI/CGI 可行性探测：7 项 R/W/+ 权限查询、3 项 HTTP Git 广告允许/拒绝通过，更新 hooks 已生成。使用上游 G3T_RC 测试覆盖机制将配置、仓库和日志放在临时目录；没有改用户 HOME、个人 Gitolite 配置或项目服务。报告在忽略的 `artifacts/p2-gitolite-probe.json`。

探测确认 Gitolite 拒绝 HTTP Git 时可能返回 HTTP 200 + pkt-line ERR，而不是 403。后续适配必须在进入后端之前显式查询 ACL，测试同时检查 Git 协议结果，不能凭 HTTP 200 宣称鉴权成功或失败。hooks 生成不代表已验证每次 push 的 ref 检查；当前没有真实 SSH daemon、Linux 容器或多用户 runtime 证据。

下一步：获准后仅推送新测试分支执行 Linux CI，修复其实际反馈；本地可继续准备 Python/Gitolite 适配用例。不能跳过 Python 完整验证直接开始 Go，也不能把这个临时探测当作部署。

## 2026-10-31：P2 Gitolite ACL 适配器（首个切片）

已写入工作区：

- `runtime/access.py` 提供身份/凭据校验、编译策略与逐仓库 `gl-conf` 指纹、维护状态、Gitolite `access -q` 查询，以及原子发布 `identity.json` 快照；身份快照不复制另一套仓库授权规则。
- ACL 查询显式把受控 access root 设为 `HOME`。这是真实查询的必要条件：Gitolite 从 `$HOME/.gitolite.rc` 与 `$HOME/.gitolite` 读取配置，若沿用运行时用户主目录，就会绕过发布时校验的目录边界。
- `tests/test_access.py` 覆盖发布门槛、owner/禁用身份、凭据哈希、未知字段、私有库不泄露、维护状态、编译策略和仓库策略篡改等 fail-closed 行为。
- `scripts/verify_gitolite_access.py` 在临时目录编译真实 Gitolite 策略并执行身份×仓库×R/W 矩阵；不写用户 HOME、个人 Gitolite 配置或真实仓库。

本轮真实证据：

- 使用 Gitolite v3.6.14（提交 `b427f928615f60d3b361472748d8a768d493bda0`）完成 **24 项检查全部通过**：公开匿名读、私有匿名拒绝、reader 只读、writer 读写、owner 读写、禁用/未知身份拒绝、哈希凭据、401/404 区分、撤权后的 writer 写入拒绝、未变 reader 保留读取、同一进程内策略文件变化后拒绝及 maintenance 返回 503。
- 报告保存在忽略的 `artifacts/p2-access.json`；其中明确标注 web、Git HTTP、LFS、SSH 尚未路由到适配器，不代表 P2 完成。

尚未接线：现有单账号 HTTP 网关/Filters 仍不调用该适配器；逐用户网页可见性、Git/LFS 入口授权、HTTP 更新 hook、SSH forced command、短期 LFS 凭证与撤钥都还没有端到端证据。下一步应先把同一适配器接到一个完整入口链并验证所有直链，不能把 24 项 ACL 查询结果外推成服务已完成授权。

## 2026-10-31：P2 全入口本地闭环

上段记录的“尚未接线”已经由后续工作补上：

- 网页列表与直接 URL、Git HTTP、LFS Batch/对象、SSH Git 和 `git-lfs-authenticate` 均调用同一 Gitolite ACL 适配器；身份撤销会同时使新 HTTP 请求、SSH 命令、LFS token 和网页会话失效。
- SSH forced command 只接受 `git-upload-pack`、`git-receive-pack` 和 `git-lfs-authenticate`；交互 shell、端口转发和任意命令拒绝。SSH 签发的 LFS token 绑定仓库、操作、身份和策略 generation，并会过期。
- 写入口在授权后持有共享写锁；权限 apply、归档、备份和同步通过 maintenance 标记与独占锁形成跨入口写栅栏。策略快照和逐仓库 `gl-conf` 指纹在查询前重新核对，异常时 fail closed。
- 真实 native 服务回归为 **69 项全部通过、无 skip**，报告在忽略目录 `artifacts/p2-native.json`。覆盖单账号旧行为、多人权限矩阵、真实 `sshd + lighttpd + cgit + Gitolite`、Git/LFS 往返、更新 hook、撤权和 SSH 主机密钥持久化。

这仍是 macOS native 临时服务的证据。Linux 镜像、Compose 多服务、Caddy 和 Actions 尚未运行，不能据此宣称已部署或 production verified。

## 2026-10-31：P3 Compose/TLS 黑盒契约（待 Linux 执行）

已把 `COMPOSE-TLS` 从空库存项补成可执行的 Linux 契约，尚不能记作功能通过：

- `tests/contracts/test_compose.py` 使用仓库现有的 base/access/public Compose 文件，追加测试覆盖：Web 后端不发布宿主端口，SSH 只发布显式端口，Caddy 使用真实证书链提供 HTTPS，Git HTTP clone/push、ACL、请求头和重启后数据仍可用。
- TLS 测试沿用生产 Caddyfile，只将证书签发替换为 Caddy `tls internal`；测试从 edge 持久目录导出内部根证书，再让 `curl` 与真实 Git 客户端按该证书验证，不关闭 TLS 校验。
- 容器 ACL/SSH 契约现在可在没有宿主 Gitolite 的 CI 中用已构建镜像内的固定 Gitolite 执行；同时修正 Docker 契约把镜像路径误写成 `/usr/bin/gitolite` 的问题，实际入口是 `/usr/local/bin/gitolite`。
- 容器 job 设置 `CGIT_TEST_GITOLITE_IMAGE=cgit:ci` 并执行 Python-complete 契约库存检查，避免 ACL/SSH 测试因宿主缺命令被静默跳过。

本地已复核：`python3 -m compileall -q runtime scripts tests`、三个入口脚本的 ShellCheck、`gitleaks detect`、`git diff --check` 通过；CGI 驱动执行 80 个测试，0 failure/error，报告仍为 `complete: false`，其中 12 条跳过记录涵盖真实 Web、SSH、Gitolite 与 Compose/TLS。`check_contracts --stage python` 现在只缺 `SECURITY-GATES`，但该命令只是库存检查。

尚未取得：Linux Docker/Compose、Caddy 实际启动、SSH 容器重启、CI 运行反馈。本机没有 Docker，不能据此宣称 Compose/TLS 已实现验收。

## 2026-10-31：P3 maintenance 与恢复契约（进行中）

工作区已写入 Caddy 公网入口、access SSH/Compose 覆盖、独立 maintenance 镜像和任务执行器：

- 任务类型包括 `check`、`archive`、restic `backup`、受控 `sync-in` 和显式登记的管理脚本；统一持久状态、超时、全局任务互斥与跨服务写栅栏。
- `sync-in` 只接受绝对本地路径、`https://` 或 `ssh://`。Git 子进程清空用户/系统 Git 配置并仅允许 `file/https/ssh` 协议，阻止 `ext::` 等 remote helper 注入；URL 中带凭证、query、fragment 或 SSH 密码时拒绝。
- `sync-in` 先在私有 work 目录校验 Git refs 与 LFS 对象，再在同一写栅栏内原子发布 refs；远端删除、历史改写、LFS 缺失或不匹配默认失败并保留旧 refs。
- `restore` 不再把“三个目录存在”当作成功。恢复后会核对 `identity.json` generation、公共编译策略、逐仓库 `gl-conf`、SSH 授权和 LFS token key，再执行完整 Git/LFS 完整性扫描；仓库清单必须与权限快照一致。
- 新增独立 `tests/contracts/test_jobs.py`，只通过任务 CLI、临时卷、真实 Git 客户端和合成 restic 调用验证 inventory、crontab、archive、backup、`sync-in` 注入拒绝与隔离恢复。`JOBS` 和 `RESTORE` 的契约库存已从空实现变为可执行入口。

本轮复核：

- `python3 -m unittest tests.test_jobs -v`：8 项通过。
- `PYTHONPATH=tests python3 -m unittest contracts.test_jobs -v`：3 项通过。
- 当时 `check_contracts --stage python` 只缺 `COMPOSE-TLS` 与 `SECURITY-GATES`；后续已补 Compose/TLS 入口，现在只缺 `SECURITY-GATES`。这一输出是库存检查，不是功能通过。

尚未取得：Linux maintenance 镜像构建、Supercronic 真实调度、真实 restic 仓库、跨容器写栅栏、Caddy TLS/端口隔离和完整恢复后客户端 clone。当前没有 Docker，且不要用合成 restic 代替真实加密仓库验收。

## 2026-10-31：P3 安全门禁证据一致性

本轮继续补强 `SECURITY-GATES`，不进入 Go 迁移：

- 修复混合报告误判：只有完整 Linux Docker 报告中的通过用例可满足门禁；CGI/native 的通过记录不能补齐缺少的容器用例。其他报告中的失败、跳过和预期失败仍会否决通过。
- 完整报告必须具有正确 layer、非零且匹配的执行数量、无失败/跳过/预期失败/预检错误，逐项结果全部通过；重复用例 ID 和非法 case 数据直接拒绝。
- 修复安全清单 4 个单元测试 ID 的 `tests.` 前缀，与 `scripts/test.py` 实际 discover 结果一致。加入真实 discovery 校验，避免合成证据与清单一起写错而自洽通过。
- 新增回归先在原实现复现报告混用与映射问题，修复后安全门禁 6 项 CLI 契约通过。Python 阶段契约库存检查无缺项，但不代表完整功能验收。

本轮全量 CGI 回归：86 项执行，75 项通过、12 条跳过记录（含整类跳过，不能按执行数简单相减），0 failure/error；报告为 `artifacts/p3-security-cgi.json`，`complete: false`。首次沙箱运行因禁止绑定回环端口失败，获准解除该限制后重跑通过。compileall、三个入口 ShellCheck 和 diff 格式检查通过。

安全门禁测试使用合成报告验证判定行为，不构成 Linux 安全验收。人工项继续标记待验证，`production_verified` 保持 false；Linux/Compose、真实 restic 与远端 Actions 仍未取得新证据。

## 完整目标进度

| 阶段 | 状态 | 剩余证明 |
|---|---|---|
| P0 规划 | 用户已授权开始 | 后续重大范围变更继续说明 |
| P1 契约/CI | 本地 native 通过，CI 待执行 | Linux 容器、远端 Actions 基线与运行反馈 |
| P2 Python 权限/SSH/LFS | 全部入口 native 通过 | Linux 容器、Compose 与 Actions 上的同一矩阵 |
| P3 Python 完整 Compose/任务/恢复 | 任务 CLI/恢复契约本地通过；Compose/TLS 已有黑盒用例 | Caddy TLS 实际执行、真实调度/restic、跨容器写栅栏、客户端恢复 |
| P4 冻结参考 | 未完成 | 完整 Python 契约与 schema/产物 |
| P5 Go 全量替换 | 未开始 | 各模块及双实现对比 |
| P6 Go 完整验收 | 未开始 | 无 Python 运行依赖、完整 CI/安全/恢复 |
| P7 交付/试点 | 未开始 | 最终产物/文档/授权部署与实际验收 |

每次更新以新命令/实际产物为证据；不要把计划、代码存在、静态检查或历史通过记录升级为更高层验收。
