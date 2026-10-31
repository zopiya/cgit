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
| P1 契约/CI | native 与 Linux Actions 通过 | 后续变更继续完整回归 |
| P2 Python 权限/SSH/LFS | native、Linux 容器与 Compose 矩阵通过 | 生产环境与人工安全验收 |
| P3 Python 完整 Compose/任务/恢复 | Linux Compose/TLS、真实 restic/隔离恢复/客户端克隆通过 | 实际定时调度、跨容器写栅栏与故障演练、人工安全项 |
| P4 冻结参考 | 未完成 | 完整 Python 契约与 schema/产物 |
| P5 Go 全量替换 | 未开始 | 各模块及双实现对比 |
| P6 Go 完整验收 | 未开始 | 无 Python 运行依赖、完整 CI/安全/恢复 |
| P7 交付/试点 | 未开始 | 最终产物/文档/授权部署与实际验收 |

每次更新以新命令/实际产物为证据；不要把计划、代码存在、静态检查或历史通过记录升级为更高层验收。

## 2026-10-31：GitHub Actions 首轮 Linux 反馈

用户已授权推送独立测试分支 `work/python-p3-actions-20261031`。首轮 [Actions 41180469044](https://github.com/zopiya/cgit/actions/runs/41180469044) 的 Python protocol job 通过；容器构建在 Gitolite 安装步骤失败，尚未执行容器协议与 Compose。固定源码归档的 SHA-256 验证通过，但 `install` 的 `/usr/bin/perl` 解释器未安装在 fetch stage，shell 报 `./install: not found`。已为该阶段补充 Perl，等待下一轮 Linux 构建确认。没有发布镜像、更新 main 或部署。

## 2026-10-31：Linux 兼容性与稳定性闭环

[Actions 41183388624](https://github.com/zopiya/cgit/actions/runs/41183388624)（`43b3758`）通过：Python protocol、两个 Linux 镜像构建、89 项完整测试（无跳过）和自动安全门禁。安全报告 `automated_ok: true`、`test_report_complete: true`，保留 `complete: false`、`production_verified: false`。

本轮按实际失败链修复：

- Gitolite fetch stage 缺少 Perl、安装目录不存在；保留固定源码和摘要验证。
- 宿主/容器路径混用、cgit 配置跨测试反复替换、命令结果类型错误；夹具显式映射临时目录，每次从基线恢复配置。
- SSH forced command 与 git-shell 冲突、新建 Alpine 账户锁定、缺少认证隔离所需 SYS_CHROOT；只解锁新建服务身份，保留 publickey-only、逐密钥 forced command、禁 shell/转发和 cap_drop ALL。
- SSH 使用独立健康检查，避免继承 Web HTTP 检查；容器重启后重新读取自动分配的宿主端口。等待有总超时并输出最后客户端错误。
- Compose 明确传入插值变量、启用 SSH profile；Caddy 测试使用当前 UID/GID，避免留下无法清理的 root 文件。匿名列表断言与公开仓库 ACL 契约一致。
- maintenance 镜像加入 CI；真实 restic 加密备份、隔离恢复、Git 克隆及恢复完整性校验通过。非 root 恢复挂载点不再要求父目录可写。
- 失败保留 Compose 服务日志，子进程设置超时，构建使用独立缓存。合成凭据、服务与卷均隔离，不访问真实用户数据。

最终复验：[Actions 41183772500](https://github.com/zopiya/cgit/actions/runs/41183772500)（`4590d54`）再次完整通过，89 项测试无跳过，包含新增非空 LFS 指针/对象和恢复字节比对；自动安全门禁通过。

仍待完成：Supercronic 实际定时与重启补偿、跨容器写栅栏/故障恢复演练、浏览器恶意内容与资源压力测试、独立保管恢复密钥、真实公网 DNS/ACME、宿主网络/容量/漏洞扫描和升级回滚。CI 合成环境通过不代替这些验收；尚未合并 main、发布镜像或部署。

## access 登录按钮（本地增量，未推送）

新增 access 页面登录链接及顶层 `/_login` Basic 挑战入口，有效 HTTP 身份固定跳转首页，不接受 LFS token 登录。配置回归 11 项、CGI 登录挑战/固定跳转/错误凭据/方法限制测试，以及真实 native cgit/lighttpd/Gitolite 的 ACL 黑盒契约通过；compileall 与 diff 检查通过。没有引入 cookie 会话或退出登录功能，尚未验证真实浏览器弹窗/凭据缓存和本次变更的 Linux Actions。此前 89 项 Linux 绿色只对应历史提交，不替代此次增量验证。


## 2026-10-31 当前工作区审计增量

详见 [审计记录](AUDIT-2026-10-31.md)。修复 sync-in 父级符号链接、同长度损坏对象复用、真实 LFS JSON envelope 和 no-checkout 下 .lfsconfig 检查失效。真实客户端回归在临时 HEAD 副本上复现失败，修复后通过。最终 native：94 项，91 通过、0 failure/error、3 跳过；报告 artifacts/audit-native-final.json，complete:false。跳过两项 Docker Compose 和一项 Linux maintenance 真实 restic。登录入口服务端/native 验证通过，浏览器 host 不可用，未验证弹窗与凭据缓存。当前无 Docker/restic，未执行本轮 Linux CI/镜像漏洞扫描，未提交/推送/部署。写栅栏、apply 并发、角色运行配置与无 access 恢复仍未闭合，不能宣称全面审计验收完成。


## Go runtime 全量替换（当前本地工作区）

实现与兼容性见 [GO-RUNTIME.md](GO-RUNTIME.md)。生产 Python 的 11 个模块已退役，Web/SSH/maintenance、配置/诊断/过滤器/日志/access/jobs 均由 cgit-runtime 提供，没有 Python fallback。Python 仅保留在外部黑盒测试和 CI 证据工具，不进入运行镜像。

验证：完整 native 报告 artifacts/go-native-final.json，41 项中 37 通过、0 failure/error、4 跳过（两项 Compose、一项两镜像无 Python 库存、一项 Linux maintenance 真 restic）；complete:false。Go 命名回归真实执行 race 与 fuzz；另外用下载并校验官方 SHA-256 的 restic 0.19.1 完成加密备份、独立目录恢复、Git clone、非空 LFS 字节比对、错误密码拒绝和恢复损坏拒绝。macOS native 不代替 Linux 容器/包组合。Linux/amd64 静态 ELF 交叉编译通过，go vet/go mod verify、ShellCheck、Go 契约库存与 diff 检查通过。

2026-10-31 的 govulncheck 1.9.0 扫描先发现新依赖 Goldmark 1.7.16 命中 GO-2026-5320，已升级到修复版 1.7.17；重扫 findings:[]，报告 artifacts/go-vuln.json 和 go-vuln-gate.json。JSON 模式即使输出发现也可能返回 0，因此新增明确拒绝 findings/空报告/截断报告的门禁，并加入 CI。该扫描不覆盖 Alpine OS 包、镜像或宿主漏洞。初次 checksum 服务 unexpected EOF 重试后通过，没有关闭校验。

本轮同时对齐 sync-in 的 LFS 实际仓库名（含 .git）路径、单账号写协调锁、apply 排空和 SSH 独立运行配置。没有搬动真实数据；旧错误 sync-in 目录需先备份和校验再人工迁入。HEAD 与 refs 跨步骤原子性、外部写入口停写、无 access 恢复验收、浏览器/资源压力/跨容器故障仍不能宣称闭合。

CI 已改用 Go 构建/漏洞扫描和原独立协议契约，要求真实 restic 与无 skip 的 Go 回归；未推送执行。未构建运行本轮 Docker/Compose、未操作 NAS/VPS、未提交或部署。此前 Python 的 94 项、历史 Linux 89 项不能代替本轮 Go 验收。


## Go Linux Actions 验收（2026-10-31）

用户授权提交、推送并在 GitHub Actions 测试。Go 迁移提交为 `41e091827741ed16b2d6f05fe3117f3775049827`；此前登录及审计的三笔提交也已推送到 `work/python-p3-actions-20261031`。本次 [Actions 41206062121](https://github.com/zopiya/cgit/actions/runs/41206062121) 最终 `completed / success`，两个测试 job 成功，发布 job 按条件跳过，没有发布镜像或部署。

下载原始产物至忽略目录 `artifacts/actions-41206062121/` 后核对：

| 证据 | 实际结果 |
|---|---|
| container.json | 41 项全部 passed，0 failure/error/skip；ok:true、complete:true；source_revision 与上述提交一致 |
| 两份 Linux/amd64 镜像 | Web 和 maintenance 构建成功，空仓库启动/健康检查通过；无 Python 可执行文件、包、旧 runtime 目录及 .py fallback 的容器检查通过 |
| Compose/TLS、SSH、Git/LFS | HTTPS clone/push、安全头、后端端口隔离、SSH 端口/重启及主机密钥、权限/撤权、LFS 真实客户端契约通过 |
| 加密恢复 | Linux maintenance 镜像中真实 restic 备份、非 root 隔离恢复、Git clone、非空 LFS 字节比对通过 |
| Go 回归与漏洞 | 命名 race 回归及路径 fuzz 实际执行，真实 Go restic 回归无跳过；govulncheck 门禁 ok:true、findings:[] |
| 安全与秘密 | security.json 的 automated_ok:true，G1–G8 自动项通过；secrets.json 为 [] |

Web 测试镜像本地 ID：`sha256:7ecbecd6781ad5d9586d75255f488c89c15285a03b6621d7beea48af547a9762`，不是已发布 registry digest。CGI 协议阶段仍是部分证据（complete:false），没有与 Linux 报告拼接为完整通过。

`container.complete:true` 仅表示该 Linux 自动测试层完整。`security.complete:false`、`production_verified:false` 仍然保留：真实浏览器、DNS/ACME、宿主隔离与容量、OS/镜像漏洞扫描、调度与重启补偿、跨容器故障、独立恢复密钥保管及部署回滚尚未据此验收。不得把本次绿色等同于 NAS/VPS 已部署或完整生产安全验收。

本条随后以仅文档的 `[skip ci]` 提交记录；该记录提交不改变已测试运行代码和配置。没有合并 main、强制推送或操作真实仓库。
