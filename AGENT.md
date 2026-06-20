# ERPNext 本地开发指南

## 当前组织方式

这个仓库既是 ERPNext fork，也是本地开发入口。后续 Codex、IDE 和终端都优先打开：

```text
/Users/bytedance/workspace/test/erpnext
```

本地启动使用源码模式，不使用 `frappe/erpnext` 官方运行镜像。Docker 只提供 MariaDB、Redis 和 bench 开发容器；ERPNext 代码直接挂载当前仓库。

## 目录分布

```text
/Users/bytedance/workspace/test/erpnext/
├── AGENT.md
├── docker-compose.yml
├── development/
│   ├── frappe-bench/              # 本地生成，已被 .gitignore 忽略
│   ├── sites/                     # 本地站点文件，挂载到容器 /sites，已忽略
│   └── logs/                      # 本地日志，挂载到容器 /logs，已忽略
├── custom_apps/
│   └── .gitkeep                   # 后续自定义 app 放这里，每个 app 建议单独 Git 仓库
├── erpnext/                       # ERPNext 业务源码
├── banking/
├── package.json
└── pyproject.toml
```

容器内路径：

```text
/workspace/erpnext
/workspace/erpnext/development/frappe-bench
/workspace/erpnext/development/frappe-bench/apps/erpnext -> /workspace/erpnext
/workspace/erpnext/development/frappe-bench/sites -> /sites
/workspace/erpnext/development/frappe-bench/logs -> /logs
/sites -> /workspace/erpnext/development/sites
/logs -> /workspace/erpnext/development/logs
```

## 站点信息

- URL: `http://development.localhost:8000`
- Site name: `development.localhost`
- 管理员账号: `Administrator`
- 管理员密码: `admin`
- Frappe branch: `develop`
- ERPNext branch: `develop`

## 启动和停止

启动：

```bash
cd /Users/bytedance/workspace/test/erpnext
docker compose up -d
```

查看状态：

```bash
cd /Users/bytedance/workspace/test/erpnext
docker compose ps
docker compose logs -f frappe
curl -I http://development.localhost:8000/login
```

停止但保留数据：

```bash
cd /Users/bytedance/workspace/test/erpnext
docker compose down
```

不要执行 `docker compose down -v`，除非明确要删除 MariaDB 数据卷并重建站点。

## 进入 Bench

```bash
cd /Users/bytedance/workspace/test/erpnext
docker compose exec frappe bash
cd /workspace/erpnext/development/frappe-bench
```

常用命令：

```bash
bench --site development.localhost list-apps
bench --site development.localhost migrate
bench --site development.localhost clear-cache
bench build --app erpnext
bench compile-po-to-mo --app erpnext --locale zh --force
```

## 中文界面

如果设置中文后 ERPNext 模块仍有大量英文，通常是本地开发环境缺少 ERPNext 的 `.mo` 翻译文件。执行：

```bash
cd /Users/bytedance/workspace/test/erpnext
docker compose exec frappe bash -lc 'cd /workspace/erpnext/development/frappe-bench && bench compile-po-to-mo --app erpnext --locale zh --force && bench --site development.localhost clear-cache'
```

然后退出重新登录或在浏览器中硬刷新。

## 自定义 App

后续自定义 app 推荐放在：

```text
/Users/bytedance/workspace/test/erpnext/custom_apps/<your_app>
```

每个 custom app 建议自己初始化 Git 仓库，ERPNext fork 只保留 `custom_apps/.gitkeep`，不要把 custom app 源码直接提交到 ERPNext fork。

安装 custom app 时，在 bench 容器内创建软链接：

```bash
cd /workspace/erpnext/development/frappe-bench
ln -s /workspace/erpnext/custom_apps/<your_app> apps/<your_app>
bench --site development.localhost install-app <your_app>
bench --site development.localhost migrate
```

## Git 工作流

当前仓库是 ERPNext fork，远端：

```text
origin git@github.com:zjcoming/erpnext.git
```

推荐长期保留上游远端：

```bash
git remote add upstream https://github.com/frappe/erpnext.git
git fetch upstream
```

同步上游：

```bash
git checkout develop
git fetch upstream
git rebase upstream/develop
git push --force-with-lease origin develop
```

本地开发 harness 只新增少量低冲突文件：

```text
AGENT.md
docker-compose.yml
custom_apps/.gitkeep
.gitignore
```

不要提交：

```text
development/frappe-bench/
development/sites/
development/logs/
custom_apps/<app>/
node_modules/
erpnext/public/dist/
```

## Fresh Machine

macOS：

```bash
git clone git@github.com:zjcoming/erpnext.git
cd erpnext
docker compose up -d
```

WSL：

```bash
cd /home/<user>/workspace
git clone git@github.com:zjcoming/erpnext.git
cd erpnext
docker compose up -d
```

WSL 中建议放在 Linux 文件系统路径，例如 `/home/<user>/workspace/erpnext`，不要放在 `/mnt/c/...`。
