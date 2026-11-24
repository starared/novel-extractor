# 本机部署与恢复

本文件描述这台服务器上的书境实例；通用发行版安装见 README.md。

## 入口与运行方式

- 目录：`/opt/novel-extractor`，Compose 项目 `novel-extractor`，服务 `extractor`。
- 域名：`https://shu.starared.win`，Nginx 提供 HTTPS 和 Basic Auth。
- 后端：`127.0.0.1:49152`，不直接暴露公网。
- Nginx 生效配置：`/etc/nginx/conf.d/shu.starared.win.conf`；仓库副本：`deploy/shu.nginx.conf`。
- 所有页面、JS、API 均转发给同一容器，宿主机源码修改不会直接上线。
- Compose 自动合并 `compose.yaml` 与 `compose.override.yaml`。后者是本机配置，连接已有的 `new-api_default` 网络。
- `.env` 的 `DEPLOY_IMAGE` 指定已构建的版本镜像；文件权限 600，不提交 Git。
- 保持非 root 用户 `65534:65534`、只读根文件系统和健康检查。
- Docker 日志轮转沿用宿主机默认：每文件 10 MB，保留 3 个。

## 数据与备份

- `./data` 绑定挂载到 `/app/data`，归属 `65534:65534`；目录 700、文件 600。
- `data/model.json` 含模型地址、模型名及密钥，不要输出到日志或提交 Git。
- `data/segments.sqlite3` 是可重建的提取缓存，存在时由现有 pre-backup.sh 使用 SQLite backup API 导出。
- Backrest 的 `daily-server` 计划备份 `/opt`，因此覆盖新的数据目录和部署记录；数据库导出位于 `/var/backups/app-dumps`，也在备份范围内。
- 这是备份覆盖配置核验，不等于已完成一次远端备份或恢复演练。
- 制作进度仍只在内存中：更新前导出需要的成果，等待正在运行的任务结束。
- 旧命名卷 `novel-extractor_model-settings` 保留为迁移时回退副本，不再接收新数据，不能当作最新备份。

## 更新

1. 检查 `git status` 和变更，运行后端及前端测试。将准备发布的代码提交为明确版本；不要把密钥或其他未确认改动一起提交。
2. 在 `/opt/backups/novel-extractor-时间/` 创建权限 700 的备份目录，保存当前源码、`.env`、Compose、Nginx 配置和当前镜像 ID；给当前镜像加唯一 rollback 标签。备份数据时停止服务，或对数据库使用一致性备份。
3. 使用每次不同的镜像标签构建（示例在项目目录执行）：

```sh
RELEASE_TAG="local-$(date -u +%Y%m%dT%H%M%SZ)-$(git rev-parse --short HEAD)"
DEPLOY_IMAGE="novel-extractor:$RELEASE_TAG" docker compose build extractor
```

4. 将 `.env` 中的 `DEPLOY_IMAGE` 更新为上述完整镜像名，执行：

```sh
docker compose config --quiet
docker compose up -d --no-build --wait --wait-timeout 60
curl -fsS http://127.0.0.1:49152/health
```

5. 若变更 Nginx，先备份生效配置，再安装 `deploy/shu.nginx.conf`，运行 `nginx -t` 成功后 `systemctl reload nginx`。验证域名未认证返回 401、认证后页面及 API 正常。
6. 更新 `deploy/current-release.json`：镜像标签、镜像 ID、源码文件 SHA256、备份目录、回滚镜像。提交这份不含秘密的记录；保留旧镜像与对应备份。

`docker compose pull` 不用于此本地源码构建部署。禁止覆盖已有版本标签；切勿在回滚保留期内清理旧镜像。

## 回滚

先保存当前数据和配置，再从 release 记录选择 `rollback_image`，更新 `.env` 的 `DEPLOY_IMAGE`，运行 `docker compose up -d --no-build --wait`。统一反代保证页面和后端同时回滚。镜像回滚不自动恢复数据；仅当数据格式不兼容且确有需要时，停服务后恢复匹配的数据快照，并恢复 UID/GID 65534 与目录/文件权限。

本次迁移也保存了原 Compose、`.env`、源码及 Nginx 配置，可从 `source-before.tar.gz` 与 `nginx-before.conf` 恢复旧布局；旧命名卷仅含迁移前数据。不要直接用旧布局覆盖迁移后的新数据。

## 本次部署记录

- 版本镜像：`novel-extractor:local-20261124T025138Z-e60d102ff2`
- 切换前镜像：`novel-extractor:rollback-20261124T025138Z`
- 私有备份目录：`/opt/backups/novel-extractor-20261124T025138Z`
- 源码与运行容器的六个应用文件逐一 SHA256 比对一致；本次只改变部署布局，复用同一应用镜像并添加不可混淆的版本标签。
- 当前源码包含之前的未提交应用修改，因此以源码快照和文件哈希为准，不能只凭 HEAD 复现。
