> 本文保留安全设计要求与 G1–G8 定义；不是部署操作入口或当前验收状态。使用指南从 [README](../README.md) 进入，当前限制见 [VALIDATION](VALIDATION.md)。

# 一体化 cgit 安全设计与验收要求

当前实现是 Go runtime。安全控制定义用于后续持续验收；实际 Linux 自动结果与生产人工缺口见 [VALIDATION.md](VALIDATION.md)。

目标是个人公网托管、私有仓库与少量协作，以及定时同步/备份所需的可验证防护。不承诺绝对安全，不用 HTTPS、容器或语言选择替代授权、恢复和运行验收。本轮没有做完整渗透测试、CVE 扫描或真实部署检查。

## 1. 保护目标与信任边界

保护私有 Git 历史和 LFS、人员/权限配置、HTTP/LFS 凭证、同步/备份凭证、SSH 主机私钥、备份及服务磁盘/可用性。

要应对公网匿名请求、凭证尝试、协作者越权、恶意 Git/README/LFS 输入、危险同步源配置，以及误操作/任务失败。代码写权限不代表能执行 hooks、提交定时任务、改变公开状态或登录容器终端。

宿主机/Docker 管理员、所有者维护脚本和服务进程属于信任边界。共享系统 Git 用户不是多人的操作系统隔离；服务进程若被攻陷，其可访问挂载会受影响。容器无法保证抵御已被控制的宿主机，部署还要覆盖宿主补丁、管理凭证和备份隔离。

本文是实施要求，不是已发现漏洞的评级清单或安全认证。具体组件支持期和漏洞状态在构建/发布时重新核对。

## 2. 实现与证据入口

| 防护 | 代码入口 |
|---|---|
| 配置失败关闭、路径与仓库约定 | internal/runtime/configure.go、core.go |
| HTTP/Git/LFS 认证、上传校验、缓存隔离 | internal/runtime/gateway.go、config/lighttpd.conf |
| 人员/公钥/Gitolite 快照与短期 token | internal/runtime/access.go |
| SSH forced command 与受限服务 | internal/runtime/ssh.go、config/sshd_config |
| Markdown 清理与有界过滤器/日志 | internal/runtime/filters.go、highlight.go |
| 任务、写入协调与 restic 恢复 | internal/runtime/jobs.go |
| 入口/角色隔离 | docker-compose*.yml、config/Caddyfile |
| 秘密与构建边界 | .gitignore、.dockerignore、CI workflow |

代码入口可用于追踪，不等于所有压力/故障场景已测。Linux 自动验收和真实部署分层记录，不能以语言选择、HTTPS 或容器替代生产验证。

## 3. 安全控制与对应验收

### S01 公网入口

- 仅发布选定的 HTTPS/证书验证入口和 Git SSH 端口；Web 后端、Caddy 管理 API、权限文件、诊断/管理接口和备份目录不可直接公开。
- 公网凭证通过 HTTPS 使用，验证跳转、证书续期和重启后状态；局域网调试配置与公网配置区分。
- Host、Origin、X-Forwarded-*、REMOTE_USER/GL_* 不作为客户端可选择的身份来源；仅信任真实受控代理链，访问地址由服务端配置。
- 从独立入口验证 Docker 端口发布及宿主防火墙效果，不能只读配置就宣称后端不可绕过。

验收：独立网络的预期端口、真实 HTTPS、后端/管理面不可达与正常 Git/LFS。

### S02 全入口统一授权

- 默认私有、未登记/失效身份拒绝访问、所有写入绑定身份；认证失败不静默降级成匿名成功。
- SSH、HTTP Git、网页和 LFS 使用同一权威 ACL；路径规范化一致，重复编码、.git 别名、嵌套库、旧 query URL 不出现旁路。
- 列表、搜索、描述/更新时间、Atom、归档、README 图片和 raw 都检查；未授权私有库不泄露存在性或名称。错误不输出秘密或内部路径。
- 用户临时 project-list/运行配置隔离，不启用跨身份页面缓存；读不到一致策略时拒绝访问。
- HTTP push 通过正确 Gitolite/hook 路径，不能只检查一次“可写”就跳过 refs 检查。排除规则与 ACL 取交集，不能被其他协议绕过。

验收：身份×仓库×入口的允许/拒绝矩阵，含用户/单把公钥/单库撤权，旧 token 无越权能力。

### S03 SSH 与命令执行

- 公钥认证；禁用密码交互、root 登录、交互 shell、PTY、端口/agent/X11 转发和可注入身份/执行配置的客户端环境变量。
- forced command 仅允许审核过的 Git/LFS 认证命令，不把 SSH_ORIGINAL_COMMAND 原样交给 shell/eval，不开放 scp/sftp 或任意扩展命令。
- 校验仓库名、参数和路径；禁止路径逃逸，审查预检到文件打开之间的竞争行为，并结合受控目录、UID/GID 和文件权限防护。
- 校验 Git 接收对象配置（如 receive.fsckObjects）及其兼容性；保持 Git/OpenSSH/Gitolite 的受支持更新。对象校验不是访问控制，也不能保证解析器无漏洞。
- 迁移前盘点 hooks/config/alternates；不直接运行旧 Forgejo 或外部仓库脚本，不让协作者上传服务器可执行 hooks。

验收：真实容器的 sshd 生效配置、命令拼接/身份伪造/路径逃逸/转发拒绝与正常 Git 回归。

### S04 凭证与秘密

- 无默认密码，每人独立可轮换/撤销的凭证；HTTP 服务器保存合适的哈希校验值，使用成熟实现，不自行设计密码算法。
- LFS token 绑定部署、人员、来源公钥、仓库、操作、有效期和策略版本；严格校验格式/算法/范围和解析大小，使用成熟库或标准机制。
- 撤权针对后续请求和新 SSH 命令；在途传输/已有连接需单独处理，不能承诺自动终止全部既有操作。
- 使用受控秘密文件，验证宿主权限和容器 UID；本地 Compose secrets 不等于自动完成加密存储或权限隔离。
- 各服务仅获必要秘密，任务不复用所有者私钥/SSH 主机私钥。日志、错误、状态、remote URL、镜像层、构建上下文和测试产物不得留凭证。

验收：用合成秘密检查产物/日志；测试过期/错签名/跨库/跨操作/跨部署和撤钥 token；凭证轮换演练。

### S05 网页与资源滥用

- 检查 README 清理、过滤器、HTML/SVG/raw/LFS MIME、nosniff、附件与必要沙箱；仓库内容不能取得服务同源脚本执行权限。
- Basic 凭证可能被浏览器自动携带，仍需考虑跨站写入；管理操作首版不暴露 HTTP，Git/LFS 校验方法/媒体类型/有 Origin 时的来源，并验证客户端兼容。
- JSON、请求头/URL、Git pack、LFS、并发、空闲连接和临时磁盘都要有边界；磁盘不足明确失败，不留下被识别为成品的半文件。
- 高成本 history/diff/blame/archive、未认证请求、SSH 握手及凭证尝试有可测保护；参数依据实际测试，不任意限速或锁内存。
- 上传、GET/HEAD/Range、取消传输均验证；哈希检查不等于读授权，不把大文件无条件整体读入网关。

验收：恶意 HTML/SVG/Markdown、异常编码、跨站请求、超限/慢速/并发/磁盘满/取消传输及真实浏览器观察。

### S06 自动化与备份

- 任务仅由所有者登记；配置/脚本目录与可 push 仓库分离，拉下来的代码、hooks、.gitmodules、.lfsconfig 不自动获得执行或任意出站能力。
- 批准出站协议、Git/LFS endpoint 和目标；限制危险 remote helper、自动子模块递归和意外重定向，不把凭证转发到另一主机。检查远端 LFS 配置访问内网/云元数据的风险。
- 地址白名单不等于网络层已经隔离；DNS/出站策略和实际目的地要验，不能关闭 TLS/SSH 主机校验来让任务跑通。
- 协作主库与拉取型库分开，传播删除/强推、备份清理独立授权；失败保留旧 refs 和上次成功备份。
- maintenance 无 Docker socket/privileged/任意宿主挂载；互斥、维护状态和超时覆盖全部写入入口。自定义脚本属于管理员信任边界，不能说路径白名单已将其完全沙箱化。
- 完整备份加密、恢复密钥独立保管；至少规划一份运行服务不能随意删除的恢复副本（离线/独立凭证/存储保留等按环境选）。不擅自购买或启用不可逆保留策略。

验收：恶意远端配置不能执行代码/带出秘密；错目标/凭证、重叠/崩溃、半成品和备份失败后清旧版被阻止；隔离恢复 Git/LFS/身份/权限。

### S07 容器与宿主

- 无 privileged、Docker socket、host PID 或无必要的 host network；最小挂载，分角色验证只读 root filesystem 的可行性。
- Web 等使用非 root；SSH 权限下降和必要 capabilities 实测，不为表面全量 cap_drop 破坏隔离机制，也不沿用过宽能力。
- 验证适用的 no-new-privileges/default seccomp 等控制，不因启动问题直接取消；rootless 可评估，不当作未测前提。
- cgroup、文件描述符、进程数、临时空间和日志容量依据测试设定；宿主 kernel/Docker 补丁、管理 SSH、防火墙纳入部署核对，未经授权不改宿主配置。

验收：UID/capabilities、挂载读写/秘密范围、网络发布和重启/恢复；不靠放宽权限才能启动。

### S08 构建、更新与供应链

- 固定可追溯源码/校验和/部署 digest；新增组件确认支持期/CVE，CI Actions 和工具下载采用可审阅固定引用及更新流程，固定不等于不更新。
- CI 只用合成仓库/秘密，发布最小权限，不可信变更不能拿正式部署/备份凭证。
- 镜像/依赖漏洞和秘密检查纳入发布流程；按实际路径解释结果，“零项”不等于绝对安全。
- 安全更新先验真实容器权限、Git/LFS/SSH/恢复再发布；保留回退，若旧版重引入已知漏洞须明确记录和处理。
- Go 迁移补 race detector、重点解析器 fuzz、govulncheck 和依赖/镜像检查；CI 不运行真实数据任务，不在有发布权限的 PR 上下文执行不可信代码。测试脚本可用 Python，生产 runtime 迁移完成须无 Python 依赖及 fallback。

验收：来源、digest、支持状态、扫描、CI 权限、完整回归及升级回退记录；不能只凭 build 成功上线。

## 4. 公网试点门槛

每项记录“已验证 / 未验证 / 不适用（理由）”，附环境、命令、产物与结果；不能将规划文字勾选为已完成。

| 门槛 | 必要证据 |
|---|---|
| G1 权限不越界 | 完整身份×仓库×入口允许/拒绝矩阵，列表/直链/撤权 |
| G2 无命令旁路 | SSH 环境/forced command/转发限制，hooks 和任务受控 |
| G3 秘密不泄漏 | 产物/日志/错误/状态检查、挂载隔离、轮换 |
| G4 公网入口正确 | HTTPS/续期、后端/管理面不可达、独立网络验证 |
| G5 恶意输入有界 | 路径/对象/网页/异常传输测试、资源观察及正常功能回归 |
| G6 自动化影响受控 | 协议/目标和恶意配置测试、互斥与失败保留 |
| G7 可恢复 | 加密备份、隔离恢复、Git/LFS/权限及真实 clone |
| G8 供应链可追溯 | 固定产物、漏洞/秘密检查、CI 权限及回退记录 |

公网试点前完成相关门槛。可达的认证绕过、私有库泄漏、任意命令执行、秘密泄漏和可复现不可恢复数据损坏等高影响问题不能以“个人用”豁免；其他未决项记录残余风险与处理安排，由用户判断，不隐藏成零风险。

安全测试仅对合成数据和授权环境执行，不做未经授权的公网扫描、爆破或 NAS 测试。无 Docker/独立网络验证时保留缺口，不用本地解析测试替代。

## 5. 日常维护与事件恢复

- 可查询服务/任务/备份最近成功时间及安全更新状态，日志有界脱敏；外部通知渠道另行授权，不自动发邮件/聊天消息。
- 核对公开库、失效人员/设备、任务目标和备份恢复能力，定期更新/轮换；周期按实际需求决定。
- 怀疑泄漏先暂停受影响入口和任务写入、保留脱敏证据；区分用户凭证、LFS 签名秘密、出站密钥、SSH 主机私钥后按影响撤销/轮换。
- 从确认可用备份恢复到隔离位置，复验权限和真实客户端；换镜像不等于已撤销恶意数据/授权修改。

## 6. 依据与适用性

- [Docker Engine security](https://docs.docker.com/engine/security/)：容器、daemon/capabilities 与宿主边界。
- [OpenSSH 配置](https://man.openbsd.org/sshd_config)、[公钥选项](https://man.openbsd.org/sshd.8)：认证、环境、强制命令/转发。
- [Caddy HTTPS](https://caddyserver.com/docs/automatic-https)：TLS 管理，不代替应用授权。
- [Git 配置](https://git-scm.com/docs/git-config)：对象校验和运行/协议设置，按目标版本检查。
- [Gitolite 更新授权](https://gitolite.com/gitolite/conf-2)、[Git LFS 认证](https://github.com/git-lfs/git-lfs/blob/main/docs/api/authentication.md)。

当前生产 runtime 使用 Go CGI/子进程；本文是安全设计要求，不代表全部部署人工门槛已通过。实际结果与未闭合项见 [验收记录](VALIDATION.md)。
