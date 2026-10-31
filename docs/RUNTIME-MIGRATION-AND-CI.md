# Python 验证、Go 交付与 GitHub Actions 测试计划

日期：2026-10-31。状态：待审阅规划；没有实现 Go runtime，也没有修改/运行远端 Actions。配套 [总体方案](PLAN.md) 与 [安全设计](SECURITY-DESIGN.md)。

## 1. 已确定的路线和范围

采用 **Python 验证完整最小方案 → 冻结行为契约 → Go 分模块替换 → 去掉运行时 Python → 容器/恢复/安全验收**。用户本轮已明确 Go 为最终方向，不再把“是否换 Go”留为未来性能测试后的选项；性能和安全收益仍须测量/验证。

Python 是功能与集成的参考实现，不是长期并行维护的第二套产品。验证只覆盖总体方案已选的个人/少量协作范围，不继续堆功能直到原型无法冻结。新增需求先进入范围清单，不能在 Go 对齐过程中不断扩大。

最终迁移的是本项目自行维护的全部运行逻辑。cgit/Git/OpenSSH/Gitolite/Caddy/Supercronic/restic 等成熟组件继续复用；Gitolite 的 Perl、cgit 的 C、必要 shell 启动脚本不属于本次语言重写。Python 可留在开发/测试机和构建阶段，最终应用与任务运行镜像不依赖 Python。

## 2. 当前证据和 CI 缺口

现有 runtime 有 gateway.py、common.py、configure.py、doctor.py、about_filter.py、source_filter.py、access_log.py 共 7 个 Python 模块。源码高亮还调用上游 syntax-highlighting.py，因此仅移除 gateway.py 不算完成迁移。

现有 `.github/workflows/docker-build.yml` 定义：本地协议/配置测试 → amd64 镜像构建 → HTTP/页面/gzip 冒烟 → Docker 驱动 Git/LFS 集成 → GHCR 发布。当前仅 push main/tag、schedule、workflow_dispatch 触发，无 pull_request；测试/构建与 packages:write/registry login 放在同一 job。这里只确认定义，不代表本次远端已跑通或有发布权限。

当前 tests/test_gateway.py 同时包含 Python 内部导入、夹具存储操作和外部协议测试；不能原样当作独立于语言的验收。CGI 驱动跳过真实 Web 检查，个别测试还用条件分支省略浏览断言；这些不能在完整 CI 中冒充全覆盖。

本机已观察到 Go、Python、Git LFS 工具可用，没有 Docker/lighttpd/cgit 可执行环境。Go 代码可以本地编译/单测，但 Linux Docker/Compose 的主验收环境计划放在 GitHub 托管 Linux runner。仍需授权提交/推送工作流后，才能获得真实 Actions 证据。

## 3. 先建立与语言无关的契约

建立可版本化的行为清单和合成夹具；每条记录目标行为、正/反例、测试入口、所属安全门槛与结果。规则来自需求和官方协议，Python 旧行为只作参考；已知缺陷不能复制成 Go 的兼容要求。

冻结范围：

- URL、Git 仓库名/.git 别名/嵌套路径和规范化规则；旧 query-string 入口的授权与错误行为。
- 人员/公钥/ACL、公开/私有、R/RW/强推策略、配置 apply/generation 和撤权语义。
- HTTP/Git/LFS 方法、状态码、必要头、错误形状、Range/HEAD、流式传输、大小与哈希校验。
- SSH forced command、Gitolite/HTTP hook、git-lfs-authenticate 和短期凭证范围。
- README/代码/LFS 卡片的可见内容、链接与安全属性；不要求不同渲染器的空白/高亮 span 逐字相同。
- repos/lfs 布局、配置/任务状态/备份清单 schema、CLI 退出码及 JSON 输出；给可持久化 schema 明确版本。
- 同步 refs/LFS 行为、任务锁/超时/重试/停机补跑、维护状态及失败后数据保留。
- 日志脱敏/轮转、权限/磁盘错误、半文件处理、取消/崩溃后的清理及恢复行为。

独立黑盒测试通过真实 HTTP、SSH、Git/Git LFS 客户端和 CLI 访问两个实现，不 import runtime 私有函数。测试可以继续用 Python，但它们不随应用进入镜像。配置纯函数等内部测试保留在各自实现中，不假装是跨语言契约。

每次对比使用独立且等价的临时数据目录、端口、秘密和签名状态，不让 Python/Go 同时写同一仓库。比较退出码、授权结果、refs、LFS hash、恢复数据及 HTML 语义；时间、随机 token/nonce、压缩编码等非确定字段按契约规范化，而不是大面积忽略差异。

## 4. Python 阶段必须验证到什么程度

先完成完整最小纵向路径：

1. Gitolite 与真实 lighttpd/SSH 的身份、ACL 和更新 hooks 验证，确认这个后端可行。
2. public/private 同站、个人/协作者 R/RW、网页列表与全部直链授权、个人 HTTP 凭证。
3. SSH 公钥、禁止 shell/转发、主机密钥持久化和 SSH→HTTPS LFS 认证。
4. Compose 的 edge/web/ssh，Caddy 测试 TLS、动态响应和大文件传输。
5. maintenance：定时/手动同步、检查、打包和版本化备份；安全 refs 更新、LFS 完整、任务互斥/失败/重启行为。
6. 隔离恢复和旧项目/Forgejo 的合成迁移夹具；S01–S08 相关控制及 G1–G8 证据。

先实现已约定的最小管理入口和内置任务；自定义脚本/对外同步也必须完成其已选范围的边界测试，不能宣称“全面原型”却留未记录的功能洞。超出首版的扩展继续留在范围外。

冻结条件：必要黑盒用例在真实 Linux Compose 中通过，无相关关键跳过或未经记录的断言省略；已知差异/风险和版本清单明确；参考镜像/源码有可追溯标识。Python 参考版本用于 CI，不默认发布到公网或迁入真实数据。

## 5. Go 迁移清单和实现方式

| 当前/将新增逻辑 | Go 交付范围 | 主要回归点 |
|---|---|---|
| configure.py | 配置解析/生成/校验、schema 和秘密引用 | 前置顺序、无秘密落入配置、坏配置拒绝、旧配置迁移 |
| common.py | 路径、仓库映射、ACL 适配、LFS 存储公共逻辑 | 编码/穿越/符号链接/竞争行为、默认拒绝、策略版本 |
| gateway.py | CGI 路由/认证、Gitolite HTTP、LFS API/对象传输 | CGI 头/环境、hook、流式/取消、Range、并发、原子落盘 |
| doctor.py | 独立 CLI 与完整性检查 | JSON/退出码、未公开仓库、全部 refs/LFS、坏/缺失数据 |
| about_filter.py | Markdown、清理、相对链接/图片转换 | XSS、默认分支/commit 链接、表格/代码块、LFS 图 |
| source_filter.py + 上游 Python 高亮 | Go 高亮、LFS 卡片/预览 | 文件名转义、大小提示、容器 CSS/HTML、原始下载 |
| access_log.py | 日志记录/脱敏/轮转 | 权限、大小边界、退出/重启、无秘密 |
| Python 阶段新增 SSH/LFS 适配 | Go forced-command 边界、认证桥/凭证 | 公钥到身份、禁用命令、过期/越范围/撤权 |
| Python 阶段新增任务/管理逻辑 | Go job/admin 命令、状态/锁/同步/备份编排 | cron 与手动同义、失败保留、恢复、取消/重启 |

建议交付一个 Go runtime 二进制、多个子命令/兼容入口，供 CGI、cgit 过滤器、SSH 和 maintenance 调用；不是建立若干微服务。源目录按实际需要放 `cmd/cgit-runtime` 与少量 `internal` 包，避免按文件名机械复制架构。

首轮 Go 保留 lighttpd 与 CGI，用标准库 net/http/cgi 或经过验证的等效 CGI 适配。Go 仍调用官方 cgit/Git/Gitolite；不自写 Git 协议，不把 CGI 改常驻 HTTP server 与语言迁移捆绑。若以后需要常驻方式，单独设计和测量。

子进程继承的可信身份/配置环境、stdin/stdout、stderr、客户端中断、退出码、子进程回收必须明确；不能为用 http.ResponseWriter 而把 Git pack/大文件整体缓冲。

README/高亮候选为 Goldmark、Bluemonday、Chroma，实施时核对维护状态/漏洞/许可证并锁定依赖版本。迁移前确定 Markdown 扩展与 sanitizer 策略，再通过语义和恶意输入夹具；不自行用正则写 HTML 清理器。必要的 CSS 适配与截图验收属于过滤器迁移的一部分。

## 6. 迁移顺序与不变量

迁移顺序建议为：配置/公共逻辑/doctor → 日志与内容过滤器 → HTTP/Git/LFS 网关 → SSH/LFS 认证与管理 → 任务/备份编排。每组作为完整纵向切片验证后再推进，Go/参考实现都标明当前支持能力，不以临时缺功能的二进制替代默认服务。

迁移期间：

- 冻结对外 URL、权限、配置 schema 与 Git/LFS 布局；不顺手改用户历史、存储命名或公开范围。
- 两种实现可用同一测试矩阵验证，但生产部署只选择一种；短期比较期不引入永久的两语言维护/回退开关。
- 已知不安全行为不“保持兼容”；修正规则先更新规范和两端用例，记录差异。
- Python/Go token 格式若需要兼容，用测试向量和独立密钥验证，不依赖生产凭证；不能共享时明确维护切换/失效处理，不隐式放宽签名验证。
- 新版改写持久状态会影响回退：尽量保持兼容 schema；确需变更先写备份/回迁步骤，不能只保留旧镜像就宣称可退。

迁移完成后删除默认分支中的旧生产 Python runtime 和默认调用入口，参考版本由 Git 历史/固定参考产物保留；Python 黑盒测试可继续维护。不保留隐藏的 Python fallback。

## 7. GitHub Actions 的职责划分

建议分成测试与发布两个职责，具体 workflow 文件数以能清晰隔离权限为准：

| 触发 | 内容 | 权限/外部影响 |
|---|---|---|
| pull_request | 文档/配置检查、对应语言单测、Linux 镜像/Compose、契约/安全回归 | 默认 contents:read，无 registry/生产/备份秘密，无发布 |
| 可信开发分支 push / 手动测试 | 同上，必要时完整 Python↔Go 比较 | 测试权限，不默认发布 |
| schedule / 手动扩展检查 | 延长 fuzz、并发/异常、漏洞更新、基准、大文件与恢复 | 只测试，不连接真实 NAS/VPS/备份仓库，不自动发包 |
| 授权的 release/tag/手动发布 | 对可信提交完成全门槛，再发布已测试镜像 | 仅发布 job 有 packages:write；不自动部署 NAS/VPS |

PR 尽量跑完整功能，慢/大规模测试再移到定期层；不能把关键权限和 SSH 测试只放定期任务。文档变更可以只跑必要文档检查，但安全/契约规范变化必须触发相应功能测试；required check 始终给出明确结果，不能因 paths-ignore 永久等待。

Actions 的 schedule 用于 CI，不承担用户服务的定时备份；GitHub 官方说明它可能延迟/丢弃，公开仓库长期不活跃还会停用。服务调度仍在 Compose maintenance 内。

### CI 验证层

1. **基础检查**：Python 编译/对应测试、shellcheck、配置/Compose schema；Go 阶段 gofmt、go vet、go test。
2. **独立契约**：相同客户端/夹具测试 Python 或 Go；比较语义与存储结果。
3. **真实镜像/Compose**：Alpine/linux/amd64、非 root/PUID/PGID、capabilities、mount/secret、空库、重启及独立 SSH host key。
4. **功能端到端**：浏览器/HTTP 内容、Git SSH/HTTP clone/fetch/push、LFS 认证与往返；每个身份的允许与拒绝。
5. **任务与恢复**：本地合成 Git 源/目标、LFS 源、临时备份仓库，验证真实调度最小路径及确定性时钟测试、冲突、锁、断网/坏凭证/中断/恢复。
6. **安全检查**：S01–S08/G1–G8、恶意输入、秘密扫描、镜像漏洞；Go race detector、govulncheck、重点 parser fuzz。
7. **性能观察**：同一 runner/夹具测启动开销、延迟、RSS/CPU、并发/大文件；基准有噪声，先记录对比，不凭单次结果许诺提升。

Go race detector 只覆盖实际运行路径，fuzz 和漏洞扫描各有覆盖边界；“全部通过”不等于无漏洞。GoCGI 多进程访问的文件锁不能仅靠进程内 race detector 证明安全。

强制规则：完整容器 job 缺 Docker/SSH/Git LFS、启动失败、测试关键用例 skip 或通过条件分支省略断言，应失败或显式未完成，不允许绿色假通过。只对明确不适用项维护可审阅例外清单。

### 夹具与成本

- 默认只测 amd64；不为迁移扩成多平台/多操作系统矩阵。Go 工具链固定已验证版本，支持期/升级另行跟进。
- 用临时测试域名和 Caddy 本地测试 CA 验证 TLS，客户端明确信任测试 CA，不全局关闭验证。不在每个 PR 请求真实 ACME 证书；真实公网证书/路由另行验收。
- 同步源和备份均使用 runner 上的合成服务/目录；外部断网/故障可控。大文件测试取足以观察流式/上限的夹具，真实多 GiB/慢速公网验证另列。
- Go fuzz 有界执行并保存最小复现 corpus；PR 与定期任务使用不同预算。镜像缓存只优化构建，不复用测试秘密/数据，PR 不写可污染发布的可信缓存。
- runner/job timeout、并发和 artifacts 保留周期按首轮耗时/费用选择，不在规划里拍数值；配置 workflow concurrency，取消旧测试不影响数据备份，因为 CI 不连接正式数据。

## 8. Actions 安全、产物和发布

- PR 与构建测试默认只读，避免在 pull_request_target 等高权限上下文 checkout/运行未信任变更。不使用生产 NAS 的 self-hosted runner 执行公开 PR。
- 固定可审阅 Actions/工具引用，最小 GITHUB_TOKEN 权限；正式发布权限/凭证只进入可信发布 job。秘密扫描不以 GitHub 自动日志遮罩为唯一防线。
- CI 输出提交 SHA、语言/版本、各门槛结果、失败阶段、测试数量/skip 清单、镜像标识与数据校验摘要；记录 run URL，不能只截绿色图标。
- 保存脱敏日志、必要 HTML/浏览器截图、JUnit/等效机器结果、fuzz 最小复现和基准；不上传完整 secrets、SSH 私钥、Authorization、完整 runner 环境或真实仓库。
- 失败时先采证再清理容器/网络/临时秘密；清理总是执行。artifacts 可被仓库读者取得且会到期，不作为用户备份或长期发布档案。
- **发布测试过的同一份镜像产物**，应用/任务镜像分别标识；不能测试后无校验地重建另一份含变化依赖的镜像再推送。构建一次、验证、记录 digest，再受控发布/提升标签；需跨 job 传递时验证来源、SHA 和 artifact 关联。
- package write/registry 权限问题与代码测试失败分开报告；旧 GHCR 包权限不根据历史直接推断现状。发布后需要真实 pull 验证；CI 构建成功仍不证明 VPS/NAS 已部署。

## 9. 阶段交付和退出条件

| 阶段 | 交付 | 退出条件 |
|---|---|---|
| P0 | 总体/安全/迁移/CI 规划 | 用户审阅确认后开始代码开发 |
| P1 | 独立行为契约、PR 测试框架与当前 Python 基线 | 本地证据明确；授权推送后有真实 Actions Linux 镜像记录，关键 skip 不算通过 |
| P2 | Python 权限、SSH、HTTP/LFS 最小闭环 | Gitolite 可行；跨入口一致、hook 不绕过、撤权与 SSH/LFS 认证通过 |
| P3 | Python Compose、自动化、备份/恢复最小完整方案 | 全范围契约与安全矩阵通过，任务和隔离恢复有真实容器证据 |
| P4 | 冻结协议/schema/夹具、参考源码/镜像 | 无未记录行为洞；明确 Go 清单和可追溯对比基线 |
| P5 | Go 全模块逐项替换 | 每组新旧契约一致或批准的安全改进；数据与权限不退化 |
| P6 | 最终 Go 镜像、完整 Actions 证据 | 生产 runtime/过滤器/任务无 Python 依赖/回退；完整 Compose、恢复、安全、race/fuzz/漏洞检查达标 |
| P7 | 最终交付、迁移/回退手册 | 隔离恢复与真实发布产物拉取验证；实际 NAS/VPS 部署另行授权与验收 |

确认后第一批只做 P1：提取客户端黑盒契约，跑当前 Python 基线，编写 PR/容器测试路径并审查权限。它不包含生产架构切换、正式仓库迁入、远端同步、备份任务启用或立即开始 Go 重写。

最终完成定义：功能/安全范围与总体方案一致；Git/LFS/配置布局兼容；web/ssh/maintenance 的本项目逻辑全部使用 Go；README/高亮也不调用 Python；运行镜像的包清单和实际功能测试均证明无 Python 依赖；留存可追溯 CI 和恢复记录。不能仅凭 `go build`、一个 HTTP 200 或移除 Python 包就宣布完成。

## 10. 当前执行与授权边界

本轮只更新规划文档，未修改现有 workflow/Go/Python/Compose 运行代码，未提交/推送/触发 Actions/发布/连接生产。用户要求先审阅，不能用“可以结合 Actions”推定已授权推送或发包。

后续阶段如本机仍无 Docker，可先准备本地检查和工作流，但不能拿静态 YAML 当容器验收。到需要 Actions 运行时，明确本地提交、推送测试分支、发布镜像、实际部署分别需要的授权；不把它们捆成一次不透明操作。

## 官方依据

- [GitHub workflow 触发](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)、[安全使用](https://docs.github.com/en/actions/reference/security/secure-use)、[artifacts](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)。
- [Go CGI](https://pkg.go.dev/net/http/cgi)、[race detector](https://go.dev/doc/articles/race_detector)、[fuzz](https://go.dev/doc/security/fuzz/)、[漏洞管理](https://go.dev/doc/security/vuln/)。
- [Goldmark](https://github.com/yuin/goldmark)、[Bluemonday](https://github.com/microcosm-cc/bluemonday)、[Chroma](https://github.com/alecthomas/chroma)：候选能力，非已安装依赖或版本决定。
