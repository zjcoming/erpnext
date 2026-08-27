# v17 固定基线与发布规则

本项目继续基于 ERPNext/Frappe v17 开发，但不再跟随浮动的 `develop` 和 `latest` 运行环境。开发阶段允许 ERPNext 与自定义应用继续产生提交；Frappe 框架、Bench 镜像和工具链必须保持在已验证基线。

## 当前基线

| 组件 | 固定值 | 约束 |
| --- | --- | --- |
| Bench 镜像 | `docker.io/frappe/bench:v5.31.0` | 禁止使用 `latest` |
| Frappe | `695b554ca883543bd6221d1f618ae5371c0dbe98` | 运行时必须精确匹配且工作区干净 |
| ERPNext 起点 | `b81d0431dd0f2557209317e6da08dd8fa57f018e` | 开发提交必须是该提交的后代 |
| Frappe/ERPNext 版本 | `17.0.0-dev` | 版本声明必须匹配 |
| Python | `3.14.2` | 必须精确匹配 |
| Node.js | `24.16.0` | 必须精确匹配 |
| 自定义应用 | `process_simplification` | 必须出现在 Bench 的 `sites/apps.txt` |

机器可读值统一保存在 `deployment/v17-baseline.env`，其中不得加入密码或其他秘密。

ERPNext 使用“固定起点”而不是把当前 HEAD 写回清单：清单本身也是 ERPNext 仓库中的文件，提交它会再次改变 HEAD。开发校验因此要求当前提交继承固定起点；正式发布则额外要求一个明确的发布 Tag 精确指向当前 HEAD。

## 日常开发

使用固定清单解析 Compose：

```bash
docker compose --env-file deployment/v17-baseline.env up -d
deployment/verify-v17-baseline.sh development
```

Compose 在创建 Bench 时仍以 `develop` 作为获取入口，随后立即把 Frappe 切换为清单里的精确提交，同时验证 ERPNext 当前 HEAD 继承固定起点。如果 `apps/frappe` 有本地修改，启动会中止，不会覆盖修改。

从独立 Git worktree 校验共享的现有 Bench 时，显式传入路径：

```bash
BENCH_ROOT=/home/frappe/projects/erpnext/development/frappe-bench \
  deployment/verify-v17-baseline.sh development
```

开发模式允许 ERPNext 工作区有未提交修改，但会给出警告；Frappe 始终不允许漂移或本地修改。

## 内部发布

1. 提交并完成测试、构建和浏览器验收。
2. 给被验收的 ERPNext HEAD 创建不可变的内部发布 Tag，例如：

   ```bash
   git tag -a internal-v17-2026.08.27.1 -m "Internal v17 release 2026.08.27.1"
   ```

3. 用该 Tag 执行生产校验：

   ```bash
   ERPNEXT_RELEASE_REF=internal-v17-2026.08.27.1 \
     deployment/verify-v17-baseline.sh production
   ```

4. 保存发布 Tag、`deployment/v17-baseline.env`、数据库备份和站点文件备份。生产部署应使用 `frappe_docker` 构建包含指定应用版本的镜像；本仓库的 Compose 是开发环境，不应直接演变为生产部署。

生产模式要求 ERPNext 工作区干净，且发布 Tag 精确指向当前 HEAD。不要用分支名作为发布版本。

## 升级基线

升级只能在独立集成分支完成，并把 Frappe 与 ERPNext 当作一组兼容版本处理：

1. 记录升级前 Tag，并备份数据库与站点文件。
2. 选择新的 Frappe、ERPNext 提交和固定 Bench 镜像 Tag；禁止直接拉取最新 `develop` 后投入日常环境。
3. 更新 `deployment/v17-baseline.env` 和 Compose 默认值。
4. 执行基线测试、`bench migrate`、资产构建、自动化测试以及关键业务页面验收。
5. 验收通过后创建新的内部发布 Tag，再进入部署。

每次升级应优先替换已经由新版本原生覆盖的自定义页面，但一次只替换一类能力，并保留数据迁移和回退窗口。

## 回退规则

- 尚未执行数据库迁移：切回上一个内部发布 Tag、对应的 Bench 镜像和 Frappe 提交，再重新构建资产。
- 已执行数据库迁移：必须同时恢复与旧 Tag 对应的数据库和站点文件备份；只回退代码可能导致 DocType/字段不一致。
- 自定义 DocType 或补丁已经写入业务数据：先验证向后迁移脚本或从成套备份恢复，不在生产库上试错。

这个方案固定的是可重复运行的技术基线，不等于把未正式发布的 v17 当成稳定版。v17 上游缺陷仍需在自定义应用或隔离补丁分支中修复，并通过新的内部发布 Tag 交付。
