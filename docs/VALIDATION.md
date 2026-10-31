# 验收与已知限制

[返回 README](../README.md) · [开发验证命令](DEVELOPMENT.md) · [历史审计](AUDIT-2026-10-31.md)

本文保留可追溯结果和当前限制，不把阶段测试流水当作操作指南。每次运行仅证明其对应源码/配置，后续修改要重新验证。

## 最近一次完整 Linux 验收

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

上述完整 Actions **没有覆盖本轮 Compose 改动**。本轮本地重新执行 native 契约：41 项中 37 passed，0 failure/error，4 明确跳过（两项 Compose、两镜像无 Python 库存、Linux maintenance 恢复），complete:false；报告 `artifacts/docs-native.json`。其中 Go race/fuzz、实际 Web/SSH/Git/LFS 与本地真实 restic 内部回归执行通过，不代替容器层。

新增示例在临时目录用合成公钥实际执行 access apply，生成两库策略、快照、授权与 token key，成功退出维护；jobs.example.json 校验通过。5 份 Compose 的 YAML 语法、Actionlint、ShellCheck、Python 编译、文档本地链接与 diff 检查通过。安全门禁 6 项定向回归通过。

按用户要求，本机不安装/运行 Docker 环境；临时 Compose CLI 下载中止且清理，未安装或执行。9 组真实 Compose 合并及自定义端口检查已加入 `scripts/check_compose.py` 和 Linux CI，但本轮尚未推送执行。当前结果不能证明新组合已在容器中运行。

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
