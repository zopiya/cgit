# 使用指南

[返回 README](../README.md) · [搭建指南](SETUP.md) · [维护备份](OPERATIONS.md)

下面使用 `demo`、`https://git.example.com` 作为示例，替换成实际地址。服务器命令在项目目录执行，客户端命令在自己的工作目录执行。

## 建库与推送

单账号或基础模式，由管理员在服务器先建 bare 仓库；HTTP push 不会自动建库：

```sh
git init --bare --initial-branch=main repos/demo.git
```

确保服务 UID/GID 有需要的权限。access 模式则在 Gitolite 策略登记仓库后 apply，详见 [SSH 与权限](ACCESS.md)。

已有本地项目推送到新建的空库：

```sh
git remote add selfhost https://git.example.com/demo
git push selfhost main
git push selfhost --tags
```

根据访问模式输入个人账号或自己的 access HTTP 凭据；使用操作系统 credential helper，避免在 URL 内嵌密码。HTTP push 必须按搭建指南开启。SSH 客户端用法见 [ACCESS.md](ACCESS.md#客户端使用)。

读取：

```sh
git clone https://git.example.com/demo
git clone --depth=1 https://git.example.com/demo shallow-demo
```

支持普通 clone/fetch、浅克隆、分支与标签。嵌套 `repos/group/demo.git` 对应 `/group/demo`；Git 和 LFS 也接受 `.git` 地址。

## GitHub 继续开发，自托管保存副本

GitHub 仍可作为 origin 开发协作，自托管作为额外 remote。日常推送所需分支和标签即可；公开实例只接收明确公开的项目及历史。不要向已有协作目标随意使用 `push --mirror`，它可能删除远端 refs。

需要独立拉取副本时可用 [sync-in 任务](OPERATIONS.md#同步副本)，默认拒绝历史改写与删除。同步可能传播错误，不能替代有历史保留的备份。

初次迁入空目标，可从旧来源创建临时完整镜像，再推送分支和标签：

```sh
git clone --mirror https://github.com/OWNER/PROJECT.git project-mirror.git
cd project-mirror.git
git remote add selfhost https://git.example.com/demo
git push selfhost 'refs/heads/*:refs/heads/*'
git push selfhost 'refs/tags/*:refs/tags/*'
```

检查目标默认分支、必要的其他 refs 和 LFS。镜像只保证 Git 对象/refs 范围，不包含独立 LFS 对象、本地仓库配置或平台附件。

## Git LFS

客户端安装 Git LFS 后执行：

```sh
git lfs install
git lfs track '*.psd' '*.zip'
git add .gitattributes
# 然后正常 add / commit / push
```

HTTP 地址自动发现 LFS；内置 SSH 使用公钥换取短期 token，实际大文件仍通过 HTTP(S) 传输。已有外部 SSH 服务也可单独配置：

```sh
git config remote.origin.lfsurl https://git.example.com/demo.git/info/lfs
```

从旧来源迁入已有 LFS 历史，在能访问旧来源的本地克隆执行：

```sh
git lfs fetch --all origin
git remote add selfhost https://git.example.com/demo  # 已添加则跳过
git lfs push --all selfhost
```

这一步只传 LFS，Git 分支/标签仍需单独推送。不要删除旧服务，直到真实 clone 得到完整大文件并通过完整性检查。

网页能显示 LFS 大小、下载/缺失状态，预览 10 MiB 内常见位图，PDF 提供打开入口。plain 路径可返回实际对象；缺失不会偷偷回源下载。**ZIP/tar.gz 快照保留 LFS 指针**，获取完整工作目录请安装 Git LFS 后 clone。

当前不支持 LFS 文件锁、其他 transfer adapter、扩展 pointer、自动 GC 或归档展开 LFS。已获仓库读权限者可以下载该仓库存储中已知 OID 的对象，不限于当前分支引用。

## 仓库展示与发布范围

在服务器上的 bare 仓库设置元数据：

```sh
git -C repos/demo.git config cgit.desc '项目说明'
git -C repos/demo.git config gitweb.owner 'owner'
git -C repos/demo.git config gitweb.category 'personal'
```

这些设置不随普通 push 传输。嵌套目录分类可能覆盖 category，必要时在 bare 仓库的 `cgitrc` 写 `section=自定义分类`。

- `cgit.hide=1` 只隐藏列表，直接 URL 仍能读取，不能用于保密。
- `cgit.ignore=1` 排除网页和 Git/LFS；只接受 `0` / `1`，非规范值明确报错。本地 cgitrc 的 `ignore` 会覆盖 Git config。
- `noweb` 标记、全局 `strict-export` 和 `project-list` 同样约束各读取入口。名单/strict-export 应写在 scan-path 之前。
- project-list 使用实际相对目录，如 `group/demo.git` 或整个 `group`，每行一项，无末尾斜杠，不允许穿越、宏、隐藏路径或符号链接。

基础和公开部署优先用物理独立的公开数据目录；需要同一实例混合公私库时用 access ACL。

浏览器提供 README Markdown、代码高亮、tree/log/diff/blame、提交搜索、Atom、patch 与快照。README 相对链接指向对应提交；原始 HTML 中自定义相对 URL 不重写。Markdown/源码过滤器输入限制 2 MiB，超限明确失败。

## 改名与平台迁移

仓库改名需一起处理 `repos/group/demo.git` 与 `lfs/group/demo.git`，停写后操作，access 模式还要更新规则并 apply，最后核对新旧地址和真实 clone。目前没有原子改名 CLI。

从 Forgejo 等平台迁来时，Issues、PR、Releases、账号和附件不会转成 cgit 功能，需要另行导出。Git config、description 和 hooks 不随 push 迁移；逐项重建，避免直接执行依赖旧平台的 hooks。迁移未验收前保留旧来源和独立备份。
