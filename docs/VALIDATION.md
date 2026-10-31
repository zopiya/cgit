# 验证记录

## 2026-10-31：P3 Compose/TLS 契约已实现，未在 Linux 执行

- 新增 `tests/contracts/test_compose.py`，从现有三份 Compose 文件启动 edge/cgit/ssh，使用 Caddy 内部 CA 作为测试信任根；不关闭客户端证书校验。
- 用例检查 Web 后端没有宿主端口、SSH 端口显式发布、HTTPS 的 401/200 与安全响应头，并通过 HTTPS 完成真实 Git clone/push；重启 edge 后重新 clone。
- 第二个用例通过真实 SSH 客户端 clone，重启 SSH 服务并核对主机密钥指纹不变。
- 容器测试可用已构建镜像内的 Gitolite 执行 access 契约，CI 设置 `CGIT_TEST_GITOLITE_IMAGE=cgit:ci`；这替换了原先会因宿主无 Gitolite 而跳过的夹具路径。
- 本机无 Docker：上述 Compose/TLS 用例只做了 Python 编译、YAML 结构检查和跳过路径验证，**没有实际启动**。
- CGI 回归：`python3 scripts/test.py --driver cgi --report artifacts/continue-current.json` 执行 80 个测试，0 failure/error；报告 `complete: false`，12 条 skip 记录覆盖真实 Web、SSH、Gitolite 和 Compose/TLS，不能作为完整服务证明。
- `compileall`、三个入口脚本的 ShellCheck、`git diff --check` 与当前提交历史范围的 `gitleaks` 检查通过。秘密扫描无发现只描述该次合成仓库扫描，不证明历史或运行环境绝对无泄漏。

## 2026-10-31：P2 Gitolite ACL 适配器首个切片

- 使用固定 Gitolite v3.6.14（提交 `b427f928615f60d3b361472748d8a768d493bda0`）在临时 Home、临时仓库与临时策略上执行 `scripts/verify_gitolite_access.py`：**24 项检查全部通过**。
- 覆盖公开匿名 R、私有匿名拒绝、reader R、writer R/W、owner R/W、禁用/未知身份拒绝；凭据正确/错误/禁用，以及匿名 401、无写权限 404。
- 修改权威策略并重新编译后，`writer` 的私有库 W 被拒绝，未变更的 `reader` 仍可 R；同一适配器实例查询成功后篡改 `gl-conf`，下一查询返回 503；进入 maintenance 也返回 503。
- 适配器每次查询前重新核对公共编译策略和仓库 `gl-conf` 指纹，避免同一长生命周期实例缓存一次成功结果后继续使用未发布规则。
- 适配器查询时显式使用 access root 作为 `HOME`，避免 Gitolite 读取运行时用户主目录中的另一份配置。`tests/test_access.py` 对该边界、同实例策略变化和快照/凭据/篡改行为有 8 项内部回归。
- 报告为 `artifacts/p2-access.json`。这是 native Gitolite CLI 适配证据，不是网页、Git HTTP、LFS 或 SSH 的端到端授权证据；适配器尚未接入现有服务。
- 验证未启动 SSH daemon、未执行真实 ref update hook、未运行 Linux 容器或 Actions，也未接触 NAS/VPS/真实用户仓库。

本次命令：

```sh
python3 scripts/verify_gitolite_access.py \
  --gitolite /private/tmp/cgit-gitolite-source/src/gitolite \
  --report artifacts/p2-access.json
```

## 2026-10-31：P1 独立契约与 CI 基础

- 临时构建真实 cgit v1.3.1（提交 044821677c774cd24f25f1818ea51d09cc64b006）与 lighttpd 1.4.82 后执行 `scripts/test.py --driver lighttpd --require-no-skips`：**42 项全部通过，无跳过**。
- 新的契约测试不导入 runtime 内部代码，通过配置 CLI、HTTP、Git 和 Git LFS 客户端验证；其余原有内部回归保留。
- 独立客户端环境不读写全局 Git 配置；LFS clone 显式声明客户端过滤器。临时 cgit 构建禁用 Lua并添加仅用于 macOS 的 memrchr 兼容定义；正式 Dockerfile 未改变，不将 native 结果当成 Linux 镜像证明。
- 先复现再修复名单末尾斜杠的问题；官方 cgit 旧 query 入口 400 与虚拟路径 404 分别精确断言，拒绝响应不得含仓库内容。
- actionlint、compileall、ShellCheck、diff 格式和文档链接检查通过。缺 Docker 预检真实失败；Python-complete 库存门槛真实拒绝未实现范围。
- 新工作流具备 PR/开发分支测试、只读测试权限及仅 main 手动发布；发布计划验证同一测试产物的校验值/镜像 ID，并按 digest 拉取复验。**尚未推送或运行远端 Actions，不能宣称该流程已通过。**
- 运行层报告在忽略的 `artifacts/` 内；任务中无真实仓库/个人密码，没有启动 NAS/VPS 服务。各阶段状态见 [PROGRESS.md](PROGRESS.md)。

本次 native 命令（临时构建目录未作为项目交付依赖）：

```sh
PATH=/tmp/cgit-native-validation/venv/bin:/opt/homebrew/bin:/usr/bin:/bin \
CGIT_TEST_LIGHTTPD=/tmp/cgit-native-validation/lighttpd/sbin/lighttpd \
CGIT_TEST_CGIT=/tmp/cgit-native-validation/cgit/cgit \
CGIT_TEST_FILTERS=/tmp/cgit-native-validation/cgit/filters \
/tmp/cgit-native-validation/venv/bin/python scripts/test.py \
  --driver lighttpd --require-no-skips --report artifacts/p1-native.json
```

## 2026-10-31：个人使用适配（本地，未部署）

- 默认 CGI 驱动：`python3 -m unittest discover -s tests -v`，31 项中 29 项通过、2 项明确跳过（真实 cgit 浏览、lighttpd gzip）。测试使用临时合成仓库；真实 Git/Git LFS 客户端克隆、浅克隆、fetch、push 和 LFS 往返通过。
- 新增 Git/LFS 排除规则检查：`cgit.ignore=1`、`noweb`、仓库本地 cgitrc 的 `ignore=1`、`project-list` 和 `strict-export`；保留 hide 仓库的直接访问。
- 在独立临时目录，把新增测试放到修改前 HEAD 的代码上执行，`noweb` 和本地 `ignore` 均复现错误的 HTTP 200；修改后这些断言返回 404。
- 错误的 `cgit.ignore=true` 和符号链接目录会阻止网页扫描；配置测试覆盖扫描约定、策略保存、前置空白/注释与缓存覆盖。
- doctor 现在包含未发布仓库的完整性检查；测试确认 ignore 仓库仍出现在检查清单中。
- `shellcheck docker-entrypoint.sh`、`python3 -m compileall -q runtime tests`、`git diff --check` 通过。
- 按官方 v1.3.1 的配置手册、`configfile.c`、`scan-tree.c`、`cgit.c` 核对解析和优先级；模板与生成配置均关闭页面缓存，默认关闭日志文件/行数统计与活动统计。未做性能基准，不报告性能收益。

本机没有 Docker、lighttpd 或可执行的 cgit：**本次没有构建镜像、验证容器启动或执行真实浏览页面断言**。新增测试中的浏览断言只在 lighttpd/Docker 驱动下执行，尚未执行；下方历史的真实服务记录不能替代本次验证。发布前应运行已有 Docker 驱动，覆盖真实 cgit 的列表、直链和 `?r=`/`?url=` 入口。

未访问 NAS、公网服务或真实用户仓库，未提交、推送、发布；备份恢复与 Forgejo 迁移只补充了操作文档，尚未实际演练。

---

以下为历史验证记录。

日期：2026-09-07（Asia/Shanghai）。状态：本地实现与验证；未提交、未推送、未发布或部署。

## 已执行

- **真实 lighttpd + cgit 集成：21 项测试全部通过，无跳过。**
  - Git 客户端浅克隆、fetch/pull、认证 push、关闭 push 时拒绝写入。
  - Git LFS 3.7.1 客户端上传/克隆往返，公开和私有模式均覆盖。
  - Batch/verify、并发上传、空对象、大小与哈希拒绝、缺失对象、HEAD/Range/If-Range。
  - 目录穿越、符号链接仓库、跨仓库对象隔离、忽略仓库、入口认证覆盖。
  - cgit 首页、日志、Atom、patch、归档、README、语法高亮、LFS 卡片与 raw 图片。
  - 解压检查归档中的 LFS 文件仍是指针；对象删除后页面及时显示缺失。
  - 动态响应的 no-store，外部 URL 配置，密码不进入生成的配置。
  - doctor 对缺失、损坏对象以及不可读取配置输出失败结果。
- `shellcheck docker-entrypoint.sh`：通过。
- `python3 -m compileall -q runtime tests`：通过。
- `git diff --check`：通过。
- 两个 Compose 文件与 CI workflow 的 YAML 解析：通过（不是 Docker Compose 运行验证）。
- 新增样式的 Impeccable 机械检查：未报告问题（不是视觉验收）。

本次本机集成命令：

```sh
PATH=/tmp/cgit-personal-build/venv/bin:$PATH \
CGIT_TEST_DRIVER=lighttpd \
CGIT_TEST_LIGHTTPD=/tmp/cgit-personal-build/lighttpd/sbin/lighttpd \
CGIT_TEST_CGIT=/tmp/cgit-personal-build/cgit/cgit \
CGIT_TEST_FILTERS=/tmp/cgit-personal-build/cgit/filters \
python3 -m unittest discover -s tests -v
```

该驱动使用仓库的 lighttpd 配置，仅替换本机路径、端口与 Python 解释器。
本机为 macOS arm64，lighttpd 1.4.82、cgit v1.3.1（固定提交）、内嵌 Git 2.54.0。
cgit 临时测试构建禁用 Lua，并仅在 `/tmp` 添加 macOS 缺少的 `memrchr` 兼容函数；该兼容修改没有进入 Dockerfile 或仓库。
运行传输的系统 Git 为 2.55.0，Python 为 3.14.4。正式产物仍仅面向 linux/amd64。

## 尚未执行

- 本机无 Docker CLI/引擎，未构建和运行 Linux 镜像，未验证 Alpine 包组合及容器入口的实际 UID/挂载行为。
- 浏览器工具的 iab 与 Chrome 均不可用，未完成桌面/移动端截图验收；页面仅有真实 HTTP/HTML 内容断言。
- 未进行真实 NAS、远程代理/TLS、真实个人仓库迁入或多 GiB 压力测试。

容器验收命令已实现，并接入发布前 CI：

```sh
docker build -t cgit:test .
CGIT_TEST_DRIVER=docker CGIT_TEST_IMAGE=cgit:test \
python3 -m unittest discover -s tests -v
```

## 审查中修复的实际问题

- doctor 原先把 `rev-list --objects` 的路径一起传给 `cat-file`，导致漏掉缺失指针；已只传 OID，并用缺失/损坏回归测试验证。
- 关闭 cgit 磁盘缓存仍会输出未来的 Expires；已在 lighttpd 对动态响应覆盖 no-store，保留静态资源行为。
- 预检 lighttpd 改为目标 UID 执行，避免预检日志文件意外成为 root 所有。
- LFS 卡片正确处理 cgit source-filter 外层 pre/code，避免将整块信息按源码空白排版。

测试使用临时合成数据与固定测试密码；没有访问或修改真实仓库。临时预览进程已停止。

## CI 反馈后的 gzip 修复（2026-09-07）

- [GitHub run 34066814640](https://github.com/zopiya/cgit/actions/runs/34066814640)：`c8aaac5` 的 Linux 镜像构建成功，但冒烟测试的 gzip 检查失败；后续容器集成检查与发布步骤被跳过。
- 根因：全局 `server.stream-response-body=2` 使 HTML 提前发送；lighttpd `mod_deflate` 要求响应完整，因此跳过压缩。`no-store` 并不是禁止压缩的原因。
- 修复：普通网页缓冲后压缩；Git RPC、LFS、raw/blob、snapshot 和 dumb HTTP 对象路径继续流式响应，上传请求也仍然流式处理。
- 新增回归测试先在旧配置下复现 `Content-Encoding` 缺失，再验证摘要/README/log/tree 的 gzip 内容可解压、no-store 保留、LFS API 与 raw 路径的 Range 下载字节正确。
- 修复后重新执行真实 lighttpd/cgit 集成：**22 项全部通过，无跳过**；独立 CGI 驱动为 20 项通过、2 项明确跳过 Web 服务专属检查。
- `git diff --check`、ShellCheck 通过。修复后的 Linux 镜像与远程 CI 尚未重新运行，仍需发布验证。
- 对 NAS 做了只读探测：`/infra/cgit/info/refs?service=git-receive-pack` 返回 `200 text/plain`，不是 Smart HTTP 推送 advertisement；该入口当前仍不支持 HTTP push。
