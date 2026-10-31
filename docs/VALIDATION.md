# 验收与已知限制

[返回 README](../README.md) · [开发验证命令](DEVELOPMENT.md) · [历史审计](AUDIT-2026-10-31.md)

本文保留可追溯结果和当前限制，不把阶段测试流水当作操作指南。每次运行仅证明其对应源码/配置，后续修改要重新验证。

## 最近一次完整 Linux 验收：性能与容量增量

[Actions 41210377194](https://github.com/zopiya/cgit/actions/runs/41210377194) 对提交 `f37078da6d39593aa7a56a478138342d43b6c41d` 完整通过。协议基线与 Linux job 成功，发布 job 跳过，没有发布或部署。

- container.json：43 项全部 passed，0 failure/error/skip，complete:true；source_revision 与上述提交一致。新增单库策略隔离、空间不足 507 且不发布半成品两条用例均明确 passed。
- 两镜像构建、Compose 配置/性能参数传递、实际 Web/Git/LFS/SSH、TLS、真实 restic 恢复及 Go race/fuzz 回归通过。
- security.json：automated_ok:true，complete:false、production_verified:false；secrets.json=[]，Go 漏洞门禁 findings:[]。
- Web 测试镜像本地 ID：`sha256:d3251009db7fb9420d5fb2c993580e70db27baf1c3643460a9db153b502c40e8`，不是已发布 registry digest。

已下载核对报告至本地忽略目录 `artifacts/actions-41210377194/`。本节由随后仅文档的 `[skip ci]` 提交记录，未修改已测试运行代码/配置。页面加速证据仍来自下方 native 基准；Linux 功能绿色不证明 100 GiB 传输、吞吐/并发容量或真实生产部署。人工缺口见文末。

## 性能与容量增量：推送前本地基线

本轮新增独立 HTTP 请求大小、空闲超时、连接数、HTTP/SSH Git pack 参数和 LFS 上传空间准入；access 单库页面只查询目标策略，首页仍检查完整可见列表。用法与限制见 [性能与容量](PERFORMANCE.md)。

最终 native 回归 `artifacts/performance-native-final.json`：43 项中 39 passed，0 failure/error，4 明确跳过（Compose 两项、两镜像无 Python 库存、Linux maintenance 恢复），complete:false。包含新的空间不足 507/不发布临时对象、单库策略隔离及既有身份/撤权/Web/Git/LFS/SSH 契约。Go race 和真实 restic 内部回归执行通过，Linux/amd64 交叉编译、vet、脚本与静态检查通过；这些不是当前 Linux 容器运行证据。

32 个业务仓库的独立 native 顺序基准中，单库 tree 授权查询 33 → 1、中位耗时 1807.748 → 257.078 ms；旧 query 同样减至 1 次，首页查询 32 次不变。报告与局部证据边界见 PERFORMANCE.md。

本轮未修改真实仓库或部署环境，未安装 Docker。本轮 Linux 结果已在本文顶部单独记录，下方更早 Actions 保留各自历史范围。没有实测 100 GiB 上传、满盘并发、网络吞吐/资源上限或文件系统配额，不能由可配置数值推导容量保证。

## 文档与 Compose 整理的 Linux 验收

本轮文档与 Compose 整理提交 `3322a51f1ce0219a9c76dcb7a24cc762e8231144` 已在 [Actions 41208433154](https://github.com/zopiya/cgit/actions/runs/41208433154) 通过（2026-10-31）。协议基线与 Linux 容器两项 job 成功，发布 job 跳过，没有发布或部署。

- 新增的 9 组 Compose 配置合并检查和独立项目/HTTP/SSH 绑定检查全部通过；这证明配置可解析及预期字段正确，不等于所有组合均启动验收。
- 完整 Linux 容器契约 41 项全部 passed，0 failure/error/skip，complete:true；报告 source_revision 与上述提交一致。
- 两镜像构建、实际 Web/Git/LFS/SSH、Compose/TLS、真实 restic 恢复和自动安全门禁通过；security 仍为 complete:false、production_verified:false，secrets.json=[]。
- 测试 Web 镜像 ID 与下方迁移验收相同；本轮未改应用镜像内容，改变的是 Compose/文档和测试门禁。

已下载核对测试/安全/镜像报告，位于本地忽略目录 `artifacts/actions-41208433154/`，并保存 CI 配置检查日志。本机未安装或运行 Docker 环境。

本节结果随后由仅文档的 `[skip ci]` 提交记录，不改变已测代码/配置。生产人工缺口仍见文末。

## Go 迁移首次完整 Linux 验收

2026-10-31，[Actions 41206062121](https://github.com/zopiya/cgit/actions/runs/41206062121)，代码提交 `41e091827741ed16b2d6f05fe3117f3775049827`。两个测试 job 成功，发布 job 跳过，未部署。

下载核对过的产物保存在本地忽略目录 `artifacts/actions-41206062121/`；CI artifact 有保留期限，长期追溯应安全保存报告。Git 中保留以下摘要：

| 验证层 | 结果和范围 |
|---|---|
| Linux container.json | 41 项全部 passed，0 failure/error/skip，complete:true，source_revision 对应上述提交 |
| 运行镜像 | Web/maintenance amd64 构建、健康检查、两镜像无 Python 库存检查通过 |
| Git/HTTP/LFS/权限 | 真实客户端、网页入口、授权拒绝与撤权、ref hook、LFS 字节与失败路径契约通过 |
| Compose/TLS/SSH | HTTPS clone/push、安全头、Web 后端无宿主端口，SSH 端口/重启/主机密钥通过 |
| restic | 真实加密备份、非 root 隔离恢复、clone 与非空 LFS 比对；错误密码/损坏拒绝回归通过 |
| Go 与依赖 | race/fuzz，govulncheck 门禁 findings:[]；不覆盖 Alpine/镜像/宿主全部漏洞 |
| 自动安全 | automated_ok:true，G1–G8 自动项通过，secrets.json=[]；security.complete:false |

Web 测试镜像本地 ID 为 `sha256:7ecbecd6781ad5d9586d75255f488c89c15285a03b6621d7beea48af547a9762`，不是已发布 registry digest。报告 production_verified:false；没有进行 NAS/VPS 真实部署。

本地 macOS arm64 native：41 项中 37 passed、4 明确 skipped，complete:false；Go race/fuzz、真实 restic 和 linux/amd64 交叉编译另行通过。本机无 Docker 引擎，因此 Linux 证据来自上述 Actions，而非本机。CGI 部分报告不与 Linux 报告拼接成虚假的完整结果。

Goldmark 初次扫描发现 GO-2026-5320 后已升级到 1.7.17，重扫无 findings。新增 JSON 漏洞门禁拒绝发现、空/截断/不完整报告；没有通过关闭校验或忽略漏洞取得绿色。

历史 Python [Actions 41183772500](https://github.com/zopiya/cgit/actions/runs/41183772500) 对应 `4590d54`，记录为 89 项无跳过，仅作历史证据；不能与 Go 用例数量直接比较。更早的阶段结果可查 Git 历史，不再作为当前部署入口。

## 当前配置/文档整理

本轮简化 README，重组搭建、配置、使用、access、维护和开发文档，移除被替代的计划/阶段流水。Compose 取消固定容器名，HTTP/SSH 支持绑定地址和端口，maintenance 拆为独立覆盖文件。新用法与旧部署命令变化见 [CONFIGURATION.md](CONFIGURATION.md)。

推送前本轮本地重新执行 native 契约：41 项中 37 passed，0 failure/error，4 明确跳过（两项 Compose、两镜像无 Python 库存、Linux maintenance 恢复），complete:false；报告 `artifacts/docs-native.json`。其中 Go race/fuzz、实际 Web/SSH/Git/LFS 与本地真实 restic 内部回归执行通过，不代替容器层。

新增示例在临时目录用合成公钥实际执行 access apply，生成两库策略、快照、授权与 token key，成功退出维护；jobs.example.json 校验通过。5 份 Compose 的 YAML 语法、Actionlint、ShellCheck、Python 编译、文档本地链接与 diff 检查通过。安全门禁 6 项定向回归通过。

按用户要求，本机不安装/运行 Docker 环境；临时 Compose CLI 下载中止且清理，未安装或执行。9 组真实 Compose 合并及自定义端口检查已加入 `scripts/check_compose.py`，随后在 Linux Actions 通过，结果见本文顶部。基础 + maintenance 等新增组合的实际调度/重启运行仍需部署演练，不能把 config 检查当成运行证据。

## 部署前仍需验证/决定

| 事项 | 实际边界 |
|---|---|
| 浏览器 Basic 登录 | 服务端链接/挑战/固定跳转已测；真实弹窗、凭据缓存、账号切换未完整验收，无退出登录系统 |
| 真实公网入口 | DNS/ACME 签发续期、独立网络后端隔离、宿主防火墙未现场验证 |
| 容量和恶意负载 | 满盘、压力、慢连接、文件描述符/进程/cgroup 限制尚无完整故障注入与容量承诺 |
| SSH 用户兼容 | 现有 UID 的 shell/锁定、非 root/rootless 和自定义根组合未全面验证 |
| 定时任务故障 | 真实时区、重启补跑、跨容器崩溃后维护恢复需演练；没有保证自动补跑 |
| 外部写入 | 宿主 SSH/脚本不遵守项目锁，备份前必须自行停写 |
| 简单部署恢复 | 项目 jobs restore 要求完整 access；无 access 使用原生 restic/归档恢复后手工验收，尚无同等独立完整恢复模式 |
| 同步发布 | LFS 先校验再 refs 事务；HEAD 单独更新，不保证 refs+HEAD 同次原子提交 |
| 老 LFS 同步路径 | Python sync-in 曾写入不带 .git 的错误目录；真实数据未自动迁移，需备份/哈希核对后人工处理 |
| 供应链 | 当前未完成 OS/镜像 CVE 扫描、SBOM、部署支持期核对；Alpine/apk 未完全锁死 |
| 灾备和回退 | 独立故障域、保留策略、离线恢复密码保管和现场回退尚未建立或验收 |

普通匿名内网、独立公开站和可选 access 均有实现；公开实例仍只能挂载明确公开的数据。试点前选定方案、核对挂载/身份/网络，验证当前产物的真实 clone/LFS 与隔离恢复，再安排实际部署。自动安全门禁不是生产安全认证。
