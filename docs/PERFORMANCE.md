# 性能与容量

[返回 README](../README.md) · [配置参考](CONFIGURATION.md)

## 默认行为

默认面向个人局域网和大文件传输：Git 使用官方传输后端，LFS 流式上传/下载，上传同时做 SHA-256、fsync 和原子发布，不将整个对象读入内存。普通网页压缩，大文件路径流式输出。日志页昂贵统计和活动统计默认关闭，源码网页展示上限 512 KB，过滤器输入上限 2 MiB。

access 单仓库页面只查询目标仓库授权；根目录首页仍逐仓库构造当前身份可见列表。目录安全扫描、目标授权及策略摘要检查保留，不缓存跨请求身份/权限，也不开共享 HTML 缓存。不会为性能关闭密码哈希、内容校验或备份协调。

没有按未知硬件强行设置 Git 线程/内存，默认保留 Git/仓库配置与自动调度。连接上限默认 1024，但这不是 1024 并发大克隆的容量承诺；每个 CGI、Git、过滤器和 SSH 进程都有资源开销。实际服务容量要在目标硬件上测量。

## 大文件配置示例

现有默认单对象上限仍为 10 GiB。100 GB 的仓库总数据与 100 GB 单个对象是不同需求；前者不受单对象大小直接限制。选择 [大文件示例](../config/performance.large-files.env.example) 中的值加入实例 `.env`，保留原身份/域名等配置：

```dotenv
CGIT_LFS_MAX_SIZE=107374182400
CGIT_HTTP_MAX_REQUEST_SIZE=214748364800
CGIT_HTTP_IDLE_TIMEOUT=900
CGIT_LFS_MIN_FREE_BYTES=5368709120
```

这允许 LFS 单对象最大 100 GiB，HTTP 请求最大 200 GiB，并在每次 LFS 上传前检查目标文件系统可用空间至少包含上传大小 + 5 GiB。只修改配置，不会预分配空间。新环境变量须用原完整 Compose 文件组合重新 up，SSH 角色也要重建配置；不要只 restart。

- HTTP 请求限制独立于 LFS 对象限制，可满足 Git pack 大于单个 LFS 对象的场景；它不是 Git pack 展开后的磁盘限额。SSH Git 不受 HTTP 大小上限限制。
- 连接/超时限制目前作用于 lighttpd，外部代理和 Caddy 的响应头等待仍有各自配置。上传文件越大，越需要核对整个链路与临时空间。
- 空间检查是准入检查，不是硬配额或空间预留；并发或外部写入仍可能耗尽磁盘，实际写盘错误会失败。不要靠它代替容量监控或文件系统配额。
- 没有项目级 Git+LFS 总容量配额。需要隔离实例/仓库占用时，使用部署文件系统/卷的配额或容量边界，并测试满额时行为；项目不擅自给宿主创建/修改配额。
- 普通 LFS 上传仍不支持分片续传，下载支持单段 Range；大上传中断可能要重新传。没有自动删除对象/GC/历史清理来腾空间。
- 参数允许到 1 TiB 只是输入校验范围，不是已实测容量保证。本轮没有传输 100 GiB 数据。

## Git 打包资源

可选设置：

```dotenv
CGIT_GIT_PACK_THREADS=2
CGIT_GIT_PACK_WINDOW_MEMORY=67108864
```

它们对项目内 HTTP/SSH Git 子进程统一注入受控的 pack.threads 和 pack.windowMemory。0 表示不覆盖 Git/仓库默认；显式正值优先于仓库配置。只使用管理员生成的参数，不接受客户端 GIT_CONFIG_* 作为调优来源。

windowMemory 约束的是每线程 delta 搜索窗口，不是整个 Git 进程的内存上限；线程数和并发会放大总资源。该参数主要影响打包路径，不等于控制接收/解包、cgit 页面或 SSH 总内存。不要把 2 线程/64 MiB 当作所有硬件最优值。

大 Git 历史还可评估 commit-graph、pack 索引与受控 repack。当前不会对真实仓库自动执行 gc/repack；先在副本测耗时、额外磁盘需求和并发影响，再安排维护。备份、检查和同步也会争用 I/O，应错开业务高峰。

## 可复现测量

`scripts/benchmark.py` 创建独立临时 bare 仓库和 access 身份，启动实际 native lighttpd/cgit/Gitolite，通过 HTTP 测量页面并计数授权后端调用；结束清理，不读取真实仓库、不安装 Docker。

准备与 native 测试相同的 CGIT_TEST_LIGHTTPD、CGIT_TEST_CGIT、CGIT_TEST_FILTERS、CGIT_TEST_GITOLITE 后：

```sh
make build
python3 scripts/benchmark.py --repositories 30 --requests 7 --report artifacts/performance.json
```

报告记录运行二进制 SHA-256、宿主平台、checkout revision/dirty、请求中位数/p95 和授权调用数。checkout revision 不等于未提交二进制所属版本；比较旧版时使用 `--runtime /absolute/path/to/reference-binary`，核对二进制摘要。少量重复请求只能作为局部基线，不作为容量 SLA；测试时应避免同时跑其他高负载任务。

本轮 macOS arm64 顺序复测，32 个业务仓库（原夹具 2 个 + 新增 30 个），每路径预热后测 7 次：

| 路径 | 修改前中位延迟 | 修改后中位延迟 | 每请求 Gitolite 授权查询 |
|---|---:|---:|---:|
| 单仓库 tree | 1807.748 ms | 257.078 ms | 33 → 1 |
| 旧 query tree | 1815.060 ms | 257.901 ms | 33 → 1 |
| 根目录首页 | 1784.303 ms | 1632.042 ms | 32 → 32 |

报告为忽略目录中的 `artifacts/performance-before-final.json` / `performance-after-final.json`，前者使用 `/tmp/cgit-runtime-before-performance` 参考二进制，后者摘要已与当前构建核对。单库页面在此夹具约降低 86% 延迟；首页算法未改变，时间差不作为收益承诺。旧二进制为本轮修改前保存的 Go 构建，当前工作区未提交，报告 checkout revision/dirty 与二进制摘要分开记录。

最终 native 契约 43 项中 39 通过、0 failure/error、4 明确容器跳过，complete:false；Go race/真实 restic 内部回归与 amd64 交叉编译通过。没有安装/执行 Docker，当前增量尚无 Linux 容器或远端 Actions 结果。完整 native/容器功能测试、吞吐/并发压力、100 GiB 容量和生产网络是不同验证层，不能互相代替。
