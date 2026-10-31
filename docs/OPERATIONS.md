# 维护与备份

[返回 README](../README.md) · [配置参考](CONFIGURATION.md) · [验收限制](VALIDATION.md)

## 日常检查

以下命令使用基础文件。若实例启用了覆盖文件，所有命令都沿用启动时完整 `-f` 组合。

```sh
docker compose ps
docker compose logs --tail=100 cgit
docker compose exec cgit cgit-doctor
docker compose exec cgit cgit-doctor --git --lfs
docker compose exec cgit tail -n 100 /var/cache/cgit/access.log
```

doctor 输出 JSON，发现问题返回非零。`--git` 执行 fsck，`--lfs` 检查所有 refs 可达标准指针及对应仓库存储对象 SHA-256，包括未发布/ignore 的 bare 仓库；不自动删除孤立对象，也不保证误删分支仍存在。大仓库完整检查可能很慢。

root 启动的容器默认 exec 是 root，检查服务真实权限时加 `--user UID:GID`。镜像健康检查只表示 Web 静态响应可用；SSH 健康检查是 sshd 配置检查，都不替代实际 clone、认证和 LFS 内容校验。访问日志约 5 × 5 MiB 轮转，Docker stderr 日志容量由宿主配置。

## 添加定时任务

任务服务独立于 access，使用单独的 maintenance 镜像，不挂 Docker socket。先准备可由服务用户读取的配置：

```sh
mkdir -p jobs job-state job-work backups secrets access
cp jobs.example.json jobs/jobs.json
```

示例只开启每天 03:00 的 integrity-check，按 `jobs.json` 的 timezone 执行；归档和加密备份默认关闭。新增 access 空目录只是此角色的挂载点，不表示基础部署必须初始化权限策略。

```sh
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  --profile maintenance up -d --build
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  exec maintenance cgit-runtime jobs validate
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  exec maintenance cgit-runtime jobs run --id integrity-check
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  exec maintenance cgit-runtime jobs status
```

若已启用 access，把 access 文件也加上。手动任务建议以服务 UID 执行（`exec --user UID:GID maintenance …`），避免 root 创建后续任务不能读取的状态文件。

Supercronic 读取启动时生成的调度表；修改任务后 restart maintenance 并核对日志和生成的 `/run/crontab`。进程存在、cron 文件存在不代表任务成功，以 status 的最近结果和实际产物为准。不承诺停机补跑；时区、跨重启执行和故障恢复需在部署环境演练。

## 同步副本

在 jobs.json 的 jobs 数组添加，例如从管理员控制的公开 HTTPS 来源同步：

```json
{
  "id": "mirror-demo",
  "type": "sync-in",
  "cron": "15 * * * *",
  "enabled": true,
  "timeout": 3600,
  "source": "https://github.com/OWNER/PROJECT.git",
  "repository": "mirror/demo",
  "lfs": true,
  "allow_rewrite": false,
  "allow_delete": false
}
```

repository 是不带 `.git` 的逻辑名，实际目标为 `repos/mirror/demo.git`。任务不自动创建目标；基础模式先在服务器创建：

```sh
mkdir -p repos/mirror
git init --bare --initial-branch=main repos/mirror/demo.git
```

access 模式先登记该目标规则并 apply，确认清单覆盖目标。同步副本不与协作者共同写同一仓库；默认分支需与来源一致。

来源只接受绝对本地目录、HTTPS 或 `ssh://` URL，不接受 scp 简写、URL 内嵌凭据、query/fragment 或 remote helper。来源 `.lfsconfig` 被拒绝；目标默认拒绝远端删除/非快进改写，失败保留旧 refs。Git/LFS 使用同一份临时 clone 清单，先发布并验证 LFS，再事务更新 refs；HEAD 单独更新，不能宣称完全原子。

需要私有来源时，应先设计专用只读出站凭据与挂载；当前示例未配置，不能把网页登录凭据当作同步凭据，也不要关闭 TLS/SSH 主机验证。维护镜像的源访问和网络隔离仍由管理员限制。

## 归档与加密备份

| 类型 | 实际内容 | 限制 |
|---|---|---|
| sync-in | Git refs 和按需同步的 LFS | 同步副本，不是历史备份 |
| archive | repos + lfs，附清单 | 未加密，不含身份/主机密钥；retain_count 只管理这类归档 |
| backup / restic | 默认 repos + lfs + access，可指定 paths | 加密历史快照；不自动包含全部配置或执行保留清理 |

开启 data-archive 会按示例在 `/backups` 保留最近 7 份归档，可能删除更老的同类归档；先确认容量和保留策略。

开启 encrypted-backup 前，用编辑器创建 `secrets/restic_password`，限制权限并保证服务 UID 可读。密码与备份库分开安全保管；丢失密码无法恢复，运行主机上同时拥有库和密码也不等于独立灾备。

默认 restic 数据为 `/repos`、`/lfs`、`/access`；无 access 部署可明确设置 `"paths": ["/repos", "/lfs"]`，但其恢复方式见下文，不能套用完整 access 恢复器。本机 `/backups/restic` 不存在配置时会自动 init，远端 restic 库须预先准备访问条件。

必须另外保存 `.env`、Compose、自定义 config、jobs 输入、HTTP 密码、SSH 主机密钥及所用镜像 digest。需要自动覆盖它们时，在 maintenance 的管理员 override 中显式增加只读挂载和 backup paths；当前默认挂载**不包含 SSH 主机密钥或全部宿主配置**。不要把备份密码作为唯一副本放进只能用该密码解密的库。

restic 任务没有内置 forget/prune 保留调度；按恢复需求另定、验证保留策略。将备份复制到独立设备/故障域，保留历史并做恢复演练。同盘 backups 只能防部分误操作。

## 写入协调与异常维护

项目内 HTTP、LFS、SSH、同步与备份使用共同协调锁：public/private 使用 `/coordination`，access 使用 `/access`。maintenance 读取 Web settings.json 判断协调根；不要在手工独立运行时指向另一份 runtime 配置。

**宿主 SSH、外部脚本等不遵循这把锁。** 备份前必须暂停这些入口。离线手工备份时，停止 Web、容器 SSH、任务和外部写入，或取得同一时间点的 Git/LFS 一致性快照。单独停止 Web 不足以停写。

异常退出可能保留 maintenance。先检查存活进程、日志、策略代际及数据完整性；access apply 失败应修正输入后重新 apply。不要靠自动删除标记或锁文件恢复访问，否则可能打破仍在执行的任务。

## 隔离恢复

永远恢复到独立空目录，核对 Git/LFS/权限和真实客户端后再安排正式替换。失败目录保留用于排障，不覆盖运行实例。

**access 完整备份**可以使用项目恢复验收器；它要求完整 access 快照、编译策略、授权、公钥 token key 和 Git/LFS：

```sh
docker compose -f docker-compose.yml -f docker-compose.access.yml \
  -f docker-compose.maintenance.yml --profile maintenance \
  exec --user 1000:1000 maintenance cgit-runtime jobs restore \
  --repository /backups/restic --password-file /secrets/restic_password \
  --snapshot latest --destination /backups/restore-drill
```

替换 UID:GID，选择不存在或为空的目标。`latest` 只用于示例，恢复特定事件时应明确选择 snapshot。此命令恢复并校验，不会自动把恢复数据切成在线服务。

**无 access 的数据备份**目前不能使用上述完整恢复验收器。停止相关外部写入后，通过 maintenance 镜像内的真实 restic 恢复：

```sh
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  exec --user 1000:1000 maintenance restic -r /backups/restic \
  --password-file /secrets/restic_password snapshots
# 确认目标不存在或为空，并用选定 snapshot ID 替换 latest：
docker compose -f docker-compose.yml -f docker-compose.maintenance.yml \
  exec --user 1000:1000 maintenance restic -r /backups/restic \
  --password-file /secrets/restic_password restore latest \
  --target /backups/restore-drill --verify
```

raw restic 只完成文件恢复，**不执行本项目完整服务验收**；archive 同样要手工解包到空目录后验收。恢复树路径随备份 paths 而异，核对后在另一个完整项目目录中接入其 repos/lfs；使用独立 cache/coordination/config 和回环端口，关闭 push，不接 SSH/同步写入。还原配置/密码、UID/GID 后执行 doctor --git --lfs，真实 clone 并比对关键分支、标签和非空 LFS 内容；access 额外验证匿名、只读、读写与撤销身份。没有这些步骤，不宣称可恢复。

恢复 SSH 主机密钥要核对原指纹；缺失时会改变服务器身份，不能让客户端盲目接受。部署切换或覆盖真实数据属于单独操作，不在恢复演练命令内。

## 升级与回退

记录当前可拉取的镜像 digest、配置、UID/GID 和完整备份。在隔离数据目录构建测试候选版本，核对启动、网页、认证、Git/LFS/SSH 和恢复，再安排正式切换。`latest` 和同名 tag 会变化，不能替代 digest。

回退镜像必须带兼容配置；它不会撤销期间的数据写入。需要回退数据时，从一致备份另行恢复。当前无自动数据迁移或 LFS GC，不主动删除对象。

旧 Python sync-in 可能写到不带 `.git` 的错误 LFS 目录；当前正式路径包含实际仓库 `.git` 名。已有这类副本时先备份、逐对象核对 SHA-256，再人工迁入正确目录，不直接删旧目录。实现/审计边界见[验证记录](VALIDATION.md)。
