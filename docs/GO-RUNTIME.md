# Go runtime 迁移

本轮将项目维护的生产运行逻辑迁移为一个 Go 二进制，源码位于 cmd/cgit-runtime 和 internal/runtime。生产构建定义与默认入口已移除 Python runtime、Python 过滤器和 Python fallback；两份运行镜像的无 Python 库存检查已在本次 Linux Actions 通过。cgit/Git/Gitolite/OpenSSH/Caddy/Supercronic/restic 保留各自职责。Python 仅留在开发机和 CI，运行独立黑盒测试及证据门禁；它不进入 Web/SSH/maintenance 运行镜像。

## 构建与入口

要求 Go 1.26+；当前工具链/镜像构建固定 1.27.3，正式目标仍仅 linux/amd64。`make build` 生成忽略的 bin/cgit-runtime；`make test` 运行 Go race 回归，`make check` 检查源码、shell 与契约库存。Go 依赖由 go.mod/go.sum 固定。配置与服务入口：

| 子命令 | 调用方 |
|---|---|
| configure | Web/SSH entrypoint，生成 cgitrc、settings.json、limits.conf |
| gateway | lighttpd gateway.cgi，路由到 cgit/Git/LFS |
| about-filter / source-filter | cgit 的 Markdown 清理、链接处理、高亮与 LFS 卡片 |
| doctor | cgit-doctor；`--git --lfs` 做完整性检查 |
| access | hash-secret、apply、snapshot、authorized-keys、init-lfs-token |
| ssh | cgit-ssh forced command，Git 与短期 LFS token |
| jobs | validate、crontab、status、run、restore |
| access-log | lighttpd 日志管道，5 × 5 MiB 轮转 |

例如 `cgit-runtime access apply --directory /access --repos /repos --identities /path/identities.json --gitolite /usr/local/bin/gitolite`。SSH 必须使用 /usr/local/bin/cgit-ssh 别名作为 authorized_keys forced command；不要把带空格的“二进制 + 子命令”塞进 --ssh-command。

## 数据与兼容性

- 不改 URL、Git bare 仓库内容、identity.json schema_version=1、PBKDF2 哈希、v1 LFS token 或 repos/lfs 挂载根。快照与 token 的规范 JSON 使用排序键、ASCII 转义，与旧格式相容。
- LFS 正式布局是 `lfs/<实际仓库相对名>/objects/xx/yy/<oid>`，如 `lfs/group/demo.git/objects/...`。旧 Python sync-in 写入的 `lfs/group/demo/` 与服务读取路径不一致；Go 同步写入正确的 `.git` 路径。本轮未搬动任何真实数据。如果已有旧同步副本，先做独立备份、核对 SHA-256，再人工决定迁入正确路径；不要直接删旧目录。
- 仅把旧模板中两个已知的 Python filter 绝对路径转换为新 Go 别名；其他 Python filter 配置拒绝启动，不悄悄退回 Python。
- Markdown 与源码过滤器输入上限为 2 MiB，超限明确失败，不静默截断。Markdown/高亮改由 Goldmark、Bluemonday、Chroma 实现，保留语义与清理边界，不承诺 HTML 字节或 CSS 配色完全一致。未完成真实浏览器截图验收。
- 运行配置是启动生成物；Web 保持缓存目录下的 runtime，SSH 改用 /run/cgit-ssh/runtime，并由 sshd SetEnv 显式传入会话，防止相互覆盖或会话清理环境后读错配置。更新环境后重新创建服务。

## 写入与维护

public/private 的项目内 HTTP/LFS 写入持共享锁，默认使用缓存旁 coordination 目录；基础 Compose 显式挂载 ./coordination:/coordination 并设置 CGIT_WRITE_ROOT。access 模式继续使用 /access 下同一把写锁。维护任务从 Web settings.json 读取协调根，手工独立执行时必须使用同一 runtime-config。宿主 SSH、外部脚本等未接入项目的写入口仍须人工停写。

apply 先创建 maintenance 标记，再等待独占写锁排空，编译、生成密钥和快照成功后才解除标记；失败保留标记。备份任务和同步互斥，并等待项目内写入结束。标记不自动强制清除：异常退出后先查存活任务、编译状态与数据完整性。

sync-in 使用同一份私有 bare clone 的 refs 与 LFS 清单，先校验/发布 LFS，再事务更新目标 refs；禁止 remote helper、嵌入凭证、远端 .lfsconfig、默认历史改写和删除。HEAD 的 symbolic-ref 仍单独更新，尚不宣称 refs+HEAD 完全原子。

archive 只覆盖 repos/lfs。restic 默认覆盖 repos/lfs/access；主机密钥、源配置和恢复秘密需显式 paths 与独立保管。restore 仍要求完整 access 快照；无 access 的服务数据恢复尚无独立验收模式。本轮没有放宽完整恢复校验来让简单部署通过。

## 测试与证据

原 Python 实现耦合的 access/gateway/jobs/token/ssh 单测随实现退役；配置的 11 项断言保留并改用 Go CLI，独立 HTTP/Git/LFS/ACL/SSH/Compose/恢复契约保留。Go 内部回归覆盖路径、哈希/token、维护锁、真实 LFS 同步、坏配置、清理与 HTML 清理，新增 fuzz。新旧用例数量不能直接比较，也不把旧 94 项绿色当成 Go 绿色。

`tests/contracts/test_go_runtime.py` 要求实际执行命名的 Go race 用例与 fuzz，Docker 契约额外检查两份运行镜像中没有 Python 包、可执行文件、旧目录或 .py fallback。scripts/test.py 报告记录生产源码摘要、Git revision 和宿主 Go 二进制 SHA-256；dirty 工作区摘要与提交 SHA 分开。镜像实际 ID 仍由独立 image smoke 报告记录。

本机没有 Docker；后续经用户授权，已在 GitHub 托管 Linux runner 构建和验证两份镜像、Compose/TLS、SSH、恢复及包库存。当前证据见本文末尾及 VALIDATION/PROGRESS；历史审计中的结论以其当时源码为准。

## 回退边界

本轮没有自动迁移真实持久数据。部署前先记录旧镜像 digest、配置、完整备份与恢复密码；回退需同时恢复兼容配置，不能只换镜像却保留新启动入口。Python 参考实现由 Git 历史 42b551d 保留，生产目录不保留隐藏 fallback。Go 已通过本轮 Linux 自动验收，真实 NAS/VPS 替换仍需独立备份、现场配置核对和部署授权。


当前本地结果：41 项 native 契约/配置中 37 通过、4 项明确跳过，0 失败；Go race/fuzz、真实 restic 加密恢复、Linux/amd64 交叉编译通过。Goldmark 已升至 1.7.17 修复 GO-2026-5320，2026-10-31 govulncheck 重扫没有 findings；JSON 扫描额外门禁会拒绝漏洞发现及不完整报告。随后 Linux Actions 已补齐 Docker/Compose、包库存和容器恢复验证；真实浏览器及生产环境仍未验证，不宣称生产就绪。详情见 VALIDATION.md。


## 已完成的远端复验

[Actions 41206062121](https://github.com/zopiya/cgit/actions/runs/41206062121) 对 Go 迁移提交 `41e0918` 完整通过：Linux 容器 41 项全部通过、无跳过，涵盖两份运行镜像无 Python、Compose/TLS、SSH/Git/LFS 和真实 restic 恢复；自动安全门禁通过。发布 job 跳过，未部署。安全报告仍为 complete:false、production_verified:false，详情及剩余人工项见 [验证记录](VALIDATION.md)。
