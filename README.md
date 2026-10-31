# cgit 轻量 Git 托管

用 cgit 浏览代码，支持 Git HTTP、Git LFS、可选 SSH 和加密备份。适合个人内网保存仓库副本，也可以独立部署为公开发布站点。

生产 runtime 使用 Go，不需要 Python、数据库或额外对象存储。当前容器支持 **linux/amd64**。不提供 PR、Issue、注册或网页管理后台。

## 快速开始

在安装了 Docker Engine 和 Compose v2 的 Linux 主机上：

```sh
cp .env.example .env
mkdir -p repos
git init --bare --initial-branch=main repos/demo.git
docker compose up -d --build
```

访问 `http://主机地址:8080/`。将代码通过你已有的受控入口写入 `repos/demo.git`，然后可以浏览或克隆：

```sh
git lfs install                  # 使用大文件时需要
git clone http://主机地址:8080/demo
```

默认无需登录即可读取、禁止 HTTP 写入，Git 仓库卷只读。8080 默认监听所有宿主地址，请在可信内网/VPN 使用，或在 `.env` 中修改绑定地址。目录权限须与 `.env` 的 `PUID` / `PGID` 对应，详见搭建指南。

## 选择用法

| 场景 | 配置 |
|---|---|
| 个人内网，匿名浏览 | 基础 Compose，写入由自己的受控入口负责 |
| 独立公开发布 | 基础 + `docker-compose.public.yml`，提供 HTTPS，只放明确公开的仓库 |
| 同一实例多人协作或混合公私库 | 基础 + `docker-compose.access.yml`，按需开启 SSH |

内网和公网实例使用独立目录、配置和密钥。不要把私有仓库挂载到公开实例，再依赖隐藏列表保护它们。简单部署不需要 access；内置容器 SSH 目前需要 access。

## 文档

- [搭建指南](docs/SETUP.md)：内网、个人 HTTP 写入、公网 HTTPS、双实例。
- [配置参考](docs/CONFIGURATION.md)：环境变量、Compose 组合、目录和反向代理。
- [使用指南](docs/USAGE.md)：建库、推送、GitHub 副本、LFS 和迁移。
- [SSH 与人员权限](docs/ACCESS.md)：首次初始化、公钥、授权、登录与撤销。
- [维护与备份](docs/OPERATIONS.md)：检查、定时任务、同步、加密备份和恢复。
- [开发与验证](docs/DEVELOPMENT.md)：Go 构建、测试、CI 和实现入口。
- [验收与已知限制](docs/VALIDATION.md)：可追溯测试结果和部署前待验事项。

当前 Go 实现已通过 [Linux Actions](https://github.com/zopiya/cgit/actions/runs/41206062121) 的 41 项测试，无跳过。该结果对应提交 `41e0918`；后续配置变更须重新验证，不能视为真实 NAS/VPS 部署验收。
