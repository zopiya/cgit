# SSH 与人员权限

[返回 README](../README.md) · [搭建指南](SETUP.md) · [维护备份](OPERATIONS.md)

这是可选的 access 模式：同一实例需要公私仓库混合、按人授权或内置容器 SSH 时使用。单纯内网浏览、独立公开发布和单账号 HTTP 写入不需要它。

HTTP 凭据和设备公钥属于同一个人员身份。Gitolite 决定仓库 R/RW 权限，`owner: true` 只标识管理身份，**不会自动授予全部仓库读写**。建库、改规则、登记人员由管理员编辑配置并 apply，没有网页管理后台或用户自助注册。

## 首次初始化

先按搭建指南准备 `.env`、repos/lfs/cache/coordination，并令 PUID/PGID 与操作用户相同。若使用专用用户，下列 `--user` 改成其实际 UID:GID，目录也要交给它。以下针对**新建、尚未运行 access 的实例**，已有策略不要覆盖。

在服务器项目目录执行：

```sh
mkdir -p access/.gitolite/conf access/.gitolite/keydir access/.gitolite/logs
cp config/access-example/gitolite.rc access/.gitolite.rc
cp config/access-example/gitolite.conf access/.gitolite/conf/gitolite.conf
cp config/access-example/identities.example.json access/identities.json
chmod 700 access
```

把你自己的设备**公钥**复制为 `access/.gitolite/keydir/owner@laptop.pub`，不要复制私钥。示例规则会创建匿名只读的 `demo` 和私有的 `private-demo`，owner 对两者都有 `RW+`；按实际需求编辑后再 apply。

```sh
ssh-keygen -lf access/.gitolite/keydir/owner@laptop.pub -E sha256
```

将输出中的 `SHA256:…` 填进 `access/identities.json` 的 keys。一个人可有多把设备公钥，每把各占一个文件和指纹；文件名为 `人员名@设备名.pub`。重复、未知人员、禁用人员或未登记指纹的公钥会使 apply 失败。

先构建镜像，再以服务用户执行初始化，不启动尚未准备好的 Web/SSH：

```sh
docker compose -f docker-compose.yml -f docker-compose.access.yml build cgit
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  run --rm --no-deps --user "$(id -u):$(id -g)" \
  --entrypoint /usr/local/bin/cgit-runtime cgit \
  access apply --directory /access --repos /repos \
  --identities /access/identities.json --gitolite /usr/local/bin/gitolite
```

成功输出 `ok:true`、generation 和 `maintenance:false`，生成编译规则、identity.json、authorized_keys 与 LFS token key。示例新库的默认 HEAD 由 Gitolite/Git 决定；对这两个尚无提交的新库，首次推送 main 前在服务器设置：

```sh
git -C repos/demo.git symbolic-ref HEAD refs/heads/main
git -C repos/private-demo.git symbolic-ref HEAD refs/heads/main
```

已有仓库按实际默认分支处理，不直接套用此命令。`identities.json` 是管理员输入，`identity.json` 是生成快照，不要混淆或手工改后者。

在 `.env` 中设置外部 Web origin（SSH LFS 必须能从客户端访问它）和需要的开关：

```dotenv
CGIT_BASE_URL=http://nas-address:8080
CGIT_HTTP_PUSH=1
CGIT_SSH_PUSH=1
CGIT_SSH_PORT=2222
CGIT_SSH_CLONE_URL='ssh://git@nas-address:2222/$CGIT_REPO_URL.git'
```

不可信网络应使用 HTTPS。access 覆盖文件固定认证模式为 access，无需再添加 write 文件。启动：

```sh
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  --profile ssh up -d --build
docker compose -f docker-compose.yml -f docker-compose.access.yml --profile ssh ps
```

默认容器 SSH 用户为 `git`，仅允许 Git 命令和 LFS 认证；没有 shell、SFTP/scp、端口/agent/X11 转发。若 PUID 命中镜像中已有账户，SSH 用户可能变为该账户名，且会保留其 shell/锁定状态；首次部署需核对日志和实际用户，不能把所有 UID 组合都当作已支持。

## HTTP 与网页登录凭据

示例 owner 最初只有 SSH 公钥，不能用它登录网页。需要网页或 HTTP 写入时，为每个人单独设置 16–1024 字节的秘密文件，通过 CLI 生成哈希：

```sh
mkdir -p secrets
# 用编辑器填写 secrets/owner-http（不在命令行或 URL 内写密码）。
chmod 600 secrets/owner-http
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  run --rm --no-deps --user "$(id -u):$(id -g)" \
  -v "$PWD/secrets:/admin-secrets:ro" \
  --entrypoint /usr/local/bin/cgit-runtime cgit \
  access hash-secret --secret-file /admin-secrets/owner-http
```

把输出的**完整哈希**作为 owner 的 `secret_hash` 字符串加入 identities.json，再运行同一条 apply。哈希有随机盐，不要照抄测试值；输入凭据、哈希文件只允许必要管理员/服务用户读取，不提交到 Git。确认凭据已安全保存后移走不需要长期留存的明文临时文件。

access 页面“登录 / Sign in”访问 `/_login`，匿名得到 Basic 挑战，有效 HTTP 身份固定跳转首页。SSH 公钥和 LFS Bearer token 不能用于网页登录。当前没有 cookie 会话、退出登录或可靠账号切换；浏览器缓存凭据的行为须现场验证，不能承诺关闭页面即退出。

## 仓库规则和人员管理

权威策略在 `access/.gitolite/conf/gitolite.conf`，名字不带 `.git`：

```conf
repo team/demo
    R = reader
    RW = writer
    RW+ = owner

repo public-demo
    R = anonymous reader writer owner
    RW+ = owner
```

R 表示读取，RW 允许普通更新，RW+ 还允许非快进/删除。具体 ref 约束由 Gitolite 规则决定，HTTP 与 SSH 更新都执行其 hook；不要把普通 RW 当作完整的受保护分支平台。

新增人员需同时登记 identities.json（enabled、keys、可选 secret_hash），添加公钥并在规则授予权限。至少保留一个 enabled owner 和一把匹配的 owner 公钥。

- 新增仓库：编辑规则后 apply，确认仓库已创建、默认分支正确，再推送。
- 撤销设备：移走其公钥文件，删除该指纹后 apply。
- 禁用人员：移走其全部公钥，设 `enabled:false`，清理规则并 apply；仅设 false 却留下其公钥会使发布失败。
- 轮换 HTTP 凭据：生成新哈希，替换后 apply。已有 LFS token 绑定 generation，不能跨策略代际绕过授权。

每次修改均运行首次初始化中那条 apply 命令。它会先进入 maintenance、等待项目内在途写入结束，再发布一致快照；**失败会保留维护标记并拒绝相关访问**。查错误、修输入后重新 apply；不要直接删 maintenance 或单独替换编译文件来恢复服务。宿主外部写入口仍需自行停写。

## 客户端使用

```sh
git lfs install
git clone ssh://git@nas-address:2222/demo.git
# 或按 HTTP 权限 clone：
git clone https://git.example.com/private-demo
```

首次连接前，通过服务器可信渠道核对 `ssh-host-keys/ssh_host_ed25519_key.pub` 的指纹：

```sh
ssh-keygen -lf ssh-host-keys/ssh_host_ed25519_key.pub -E sha256
```

不要为连接成功永久关闭主机密钥校验。SSH 重建应复用该目录，否则客户端会提示主机身份变化。SSH 只负责 Git 和签发短期 LFS token，大文件仍需访问配置的 Web 地址。

## 启用公网 HTTPS

初始化后，在相同 access 命令中再追加 `-f docker-compose.public.yml`，设置 CGIT_DOMAIN/CGIT_ACME_EMAIL，并把 SSH 服务的 CGIT_BASE_URL 也明确设为对应 HTTPS origin。public 文件只给 Web 注入 origin，不替 SSH 设置它。

SSH 是独立发布的端口，不经过 Caddy。若只允许 VPN 写入，设置 `CGIT_SSH_BIND` 为 VPN/内网地址并检查宿主防火墙。Web、Git HTTP、LFS、SSH 各入口都要分别验证允许与拒绝。
