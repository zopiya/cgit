# cgit Docker 部署

[cgit](https://git.zx2c4.com/cgit/) `v1.3.1` + lighttpd，端口 `8080`，纯 HTTP，无外部依赖。

镜像由 CI 自动构建发布：`ghcr.io/zopiya/cgit`

## 快速开始

```sh
docker compose up -d
```

把 bare repo（`myproject.git`）放进 `./repos/` 目录即可自动发现，无需手动配置。

访问 `http://<ip>:8080/`。

## 目录结构

```
./repos/    → 容器内 /repos（只读，放 bare repo）
./cache/    → 容器内 /var/cache/cgit（缓存 + access log）
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `PUID` | 1000 | 运行用户 uid |
| `PGID` | 1000 | 运行用户 gid |
| `TZ` | Asia/Shanghai | 时区 |

## 配置

- `config/cgitrc` — 主配置，烘焙在镜像里，可 bind-mount 覆盖
- `config/lighttpd.conf` — CGI 路由，一般不用改
- `config/custom.css` — 自定义样式，叠加在 cgit 默认样式之后

单仓库设置直接写 git config，无需重启：

```sh
git -C repos/myproject.git config cgit.desc "我的项目"
git -C repos/myproject.git config gitweb.owner "zopiya"
git -C repos/myproject.git config gitweb.category "infra"
git -C repos/myproject.git config cgit.hide 1   # 隐藏但可访问
```

## 已启用功能

README 渲染（Markdown → HTML）、语法高亮（pygments）、提交活跃图表、
owner 列、分支按活跃排序、`.git` 后缀自动去除、`enable-git-config`、
`section-from-path`、gzip 压缩、clone-url 跟随 Host header。

克隆开箱即用（dumb HTTP），不支持 push（走 SSH）。

## CI

每次 push `main` 自动：构建 → 冒烟测试 → 发布到 GHCR。

镜像 tag：

- `latest` / `main` — 浮动，跟踪 main
- `<date>-<sha>`（如 `20260726-85c44e1`）— 固定，可追溯
- `<version>` — 仅 `v*` tag 推送时

每周一自动重建，跟进 Alpine 安全更新。

## 升级 cgit / Git

改 `Dockerfile` 里的 `CGIT_VERSION` + `CGIT_COMMIT` 和 `GIT_VERSION` + `GIT_SHA256`，
push 到 main 即可，CI 自动处理。

`CGIT_COMMIT` 获取方式：

```sh
git clone --depth 1 --branch v<version> https://git.zx2c4.com/cgit /tmp/c
git -C /tmp/c rev-parse HEAD
```

`GIT_SHA256` 获取方式：

```sh
curl -fsSL https://www.kernel.org/pub/software/scm/git/sha256sums.asc | grep "git-<version>.tar.xz"
```
