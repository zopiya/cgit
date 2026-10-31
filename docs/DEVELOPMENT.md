# 开发与验证

[返回 README](../README.md) · [验收记录](VALIDATION.md)

## 运行结构

```text
HTTP(S) → lighttpd → Go CGI gateway → cgit / git-http-backend / LFS
SSH → OpenSSH → Go forced command → Gitolite + Git / LFS token
maintenance → Supercronic → Go jobs → check / sync / archive / restic
```

生产自有逻辑只有一个 `cgit-runtime` Go 二进制，没有 Python fallback。cgit、Git、Gitolite、OpenSSH、Caddy、restic 继续负责各自协议；Python 仅作为开发/CI 外部黑盒测试及证据工具，不进入运行镜像。

| 入口 | 实现位置 |
|---|---|
| 子命令与别名 | `cmd/cgit-runtime/main.go` |
| 配置、路径、Git 子进程 | `internal/runtime/core.go`、`configure.go` |
| Web/Git HTTP/LFS | `internal/runtime/gateway.go` |
| 人员快照、Gitolite、短期 token | `internal/runtime/access.go` |
| SSH forced command | `internal/runtime/ssh.go` |
| Markdown/高亮/访问日志 | `internal/runtime/filters.go`、`highlight.go` |
| 完整性与维护任务 | `internal/runtime/doctor.go`、`jobs.go` |
| 内部回归 / 外部契约 | `internal/runtime/runtime_test.go` / `tests/contracts/` |

镜像仅支持 linux/amd64。Go 模块最低声明 1.26，当前 CI/镜像工具链固定 1.27.3；依赖在 go.mod/go.sum。开发测试驱动要求 Python 3.11+，还需要 Git、Git LFS、ShellCheck；完整验收需要 Docker/Compose 和真实 restic。

## 本地命令

```sh
make build
make check
CGIT_TEST_RESTIC=/absolute/path/to/restic make test
python3 scripts/test.py --driver cgi --report artifacts/local.json
```

restic 路径必须指向真实客户端。普通 make test 缺少它可能跳过对应内部用例，不能作为完整验收。CGI 驱动不含实际 Web/容器服务，其 complete:false 和跳过项必须保留；native lighttpd 也不能证明 Linux 镜像行为。

`python3 scripts/check_compose.py` 在具备 Compose 的主机解析 9 种文档组合与自定义端口，不启动容器；本机不安装 Docker/Compose，该项由 Linux CI 执行。

CI 是完整构建/测试命令的权威入口，见 [workflow](../.github/workflows/docker-build.yml)。本地具备 Docker 后可复现核心容器链：

```sh
docker build --platform linux/amd64 -t cgit:test .
docker build --platform linux/amd64 -f Dockerfile.maintenance -t cgit-maintenance:test .
CGIT_TEST_IMAGE=cgit:test CGIT_TEST_GITOLITE_IMAGE=cgit:test \
CGIT_TEST_MAINTENANCE_IMAGE=cgit-maintenance:test CGIT_TEST_RESTIC=/absolute/path/to/restic \
python3 scripts/test.py --driver docker --require-no-skips --report artifacts/container.json
```

测试只使用临时合成数据，不指向真实仓库。`contracts/runtime.json` 和 `contracts/security.json` 规定库存/门禁；静态清单存在不等于路径已执行。安全门禁要求当前完整报告、对应测试 ID 和产物，不能借用历史部分报告拼出通过。

## CI 与发布

PR、main/work 分支 push、定期和手动事件运行协议基线，再构建两个 amd64 镜像并执行实际 Web/SSH/Compose/TLS/恢复契约。Go race/fuzz、govulncheck、秘密扫描与自动安全门禁均需通过；完整容器验收不接受跳过。漏洞 JSON 还通过单独脚本检查，不能仅依赖扫描器退出码。

测试 job 只有 contents:read，不使用真实部署/备份秘密。只有 main 的手动 workflow_dispatch 且 publish=true 才发布经过测试的同一 Web 镜像；不会部署。maintenance 镜像目前构建测试，但没有对应发布步骤。不要把 ghcr.io 的同名 tag 当成已发布的新 maintenance 产物。

升级 cgit 时核对 Dockerfile 的 CGIT_VERSION/COMMIT、配套 Git 源码版本和 SHA256；Git HTTP 使用的 Alpine Git 包与构建 cgit 的源码不是同一个依赖。固定引用不代替定期漏洞检查。Go 扫描不覆盖 OS 包/宿主漏洞。

## 兼容与变更记录

Go 迁移保留仓库 URL、bare 数据、identity schema_version=1、PBKDF2 哈希及 v1 token 格式。Markdown 引擎改变，保留语义与清理边界，不保证 HTML/CSS 逐字相同；过滤器上限 2 MiB。已知两个旧 Python filter 路径会转换为 Go 别名，其他 Python filter 配置明确拒绝。

旧 Python 参考实现保存在 Git 历史 `42b551d`，不放回生产目录作 fallback。此前计划、阶段流水和已被实现替代的设计稿已从现行文档移除，可通过 Git 历史查阅。当前操作只以 README 链接的指南为准；[安全设计要求](SECURITY-DESIGN.md) 保留信任边界和门槛定义；审计历史与证据见 [VALIDATION.md](VALIDATION.md) 和 [AUDIT-2026-10-31.md](AUDIT-2026-10-31.md)。
