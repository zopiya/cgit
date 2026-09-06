# 验证记录

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
