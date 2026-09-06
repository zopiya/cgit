# 个人 cgit 功能完善计划

实施状态：下列工作项已落地；本地真实服务测试通过。容器与视觉验收边界见 [VALIDATION.md](VALIDATION.md)。

范围：个人使用、linux/amd64；不增加 ARM64、团队权限、PR/Issue、CI 调度或文件锁。

## 设计

保留单镜像、lighttpd 单进程及现有 cgit 页面。增加一个 Python CGI 入口，统一处理认证、Git Smart HTTP、LFS Basic API 和 LFS 原始文件下载，其余请求执行原版 cgit。复用镜像已有 Python，不引入数据库、Web 框架或进程管理器。Git 传输委托官方 `git-http-backend`，不自行实现 Git 协议。

LFS 实现限于官方 Batch + Basic transfer（SHA-256、上传、下载、verify、Range）；每个仓库独立目录，流式写入、哈希校验、原子落盘。不实现锁、对象删除、自动 GC、远程按需拉取或 LFS 展开归档。标准 ZIP/tar.gz 仍包含 Git 指针，页面及文档明确说明。

## 默认行为与兼容

- 保留 `/repo` 和嵌套仓库 URL；同时接受 `/repo.git` Git/LFS 地址。
- `/repos` 继续只读，普通克隆自动走 Smart HTTP，旧 dumb HTTP 路径仍由 cgit 提供。
- 新增 `/lfs` 持久卷；只读下载默认可用。未配置账号时所有 HTTP 写入拒绝。
- 单一个人账号：可选择公开读取、认证写入，或全站私有。密码通过挂载文件提供，认证覆盖 Git、LFS 和所有动态页面。
- HTTP push 默认关闭；需显式开启，并使仓库卷可写。现有 SSH push 保持独立。
- 外部地址显式配置，拒绝从任意代理头推断协议；无配置时使用当前 HTTP Host。部署于域名根路径。
- 不改用户仓库配置、hooks、历史和已有对象；不自动提交、推送或部署本项目。

## 工作项

1. CGI 入口、配置生成、个人认证和 Smart HTTP 路由。
2. LFS 上传/下载/校验与持久化，兼容标准客户端自动发现。
3. LFS 文件页面与 raw 下载、README 相对链接/图片、文件重命名历史入口和并排 diff。
4. 操作诊断与 LFS 校验工具、备份恢复/迁入说明、日志轮转、可配置克隆地址。
5. 合成仓库验证：浅克隆、fetch、push 开关、认证、LFS 往返、错误对象拒绝、缺失/Range、路径隔离、README 与旧浏览功能。
6. CI 在镜像发布前运行功能测试；只保持 amd64。

## 验收与证据

本地运行独立单元/集成测试，实际调用 Git 和 Git LFS 客户端。容器检查必须通过 Docker 构建和 smoke tests 证明，不能以本地通过替代；若执行机器无 Docker，记录该限制并保留可复现命令。最终审查实现和计划的对应关系，记录尚未验证的层级。

协议依据：
- https://git-scm.com/docs/git-http-backend
- https://github.com/git-lfs/git-lfs/blob/main/docs/api/batch.md
- https://github.com/git-lfs/git-lfs/blob/main/docs/api/basic-transfers.md
- https://github.com/git-lfs/git-lfs/blob/main/docs/api/server-discovery.md
