# 交付说明：完成项、实测结果、未完成事项

交付日期：2026-11-24 ｜ 技术栈：Python 3.11 + Flask 3.1 + SQLite（WAL）+ 原生 HTML/CSS ｜ 部署：Dockerfile + docker-compose.yml

---

## 一、需求完成项对照

| # | 需求 | 实现位置 | 状态 |
|---|------|----------|------|
| 1 | Flask 供数页 | `app/routes.py`，首页 `/` 即供数总览；另有 `/api/state`、`/api/index` | ✅ |
| 2 | SQLite 存图版和供数索引 | `plates`、`refs` 存图版与引用；`index_versions` + `index_entries` 存**已发布供数索引**；`plate_status` 存暂缓状态 | ✅ |
| 3 | 上传 CSV：图版号、题名、引用号列表 | `app/graph.py: parse_catalog`；页面 `/upload`，支持文件与粘贴；支持 `; \| , ， 、 空格` 分隔、宽表多引用列、无表头、UTF-8/BOM/GBK | ✅ |
| 4 | 同号整条替换 | `catalog.apply_changes` 中 UPSERT plates + 先全删后插 refs；有测试 `test_same_number_is_whole_record_replacement` | ✅ |
| 5 | 按号撤下 | 首页撤下框（多个号分隔）、每行「撤下」按钮、`POST /withdraw`（`number` / `numbers` / 撤下名单文件） | ✅ |
| 6 | 供出引用链无缺件的所有图版 | 从全部缺失号沿反向边传播，未被命中者供数（`graph.recompute_states`） | ✅ |
| 7 | 齐备的互引图版一同供出 | 纯环不到缺件即全部供数；有互环与「环上带缺件」两个测试 | ✅ |
| 8 | 撤下后直接/间接引用者退出；补齐自动恢复 | 撤下删行→该号成缺件根→反向传播；补录后重算恢复。有端到端测试与网页实测 | ✅ |
| 9 | 只重算与变动图版相连部分 | `graph.compute_scope`；只对 scope 写状态/索引条目，scope 外从旧索引结转；页面与 `/api/state` 展示本次 scope | ✅ |
| 10 | 圈定范围：变动前后引用都计入、方向不限 | scope = 旧库所有边 ∪ 本次新引用边的无向闭包（出入边都遍历）；有专门测试 | ✅ |
| 11 | 图版改动和索引一同生效，失败仍供原索引 | 单个 `BEGIN IMMEDIATE` 事务内完成删改、状态、新版本、`active_index_version` 指针切换；异常 ROLLBACK。有注入失败的回滚测试 | ✅ |
| 12 | 网页查看供数和暂缓图版 | 首页四张统计卡 + 供数表 + 暂缓表（含 `A1→A3→A4（缺件）` 断链路径）+ 缺件表 + 版本史；`/plate?n=` 详情页 | ✅ |
| 13 | 下载 CSV 直接读已发布索引 | `/download.csv`（别名 `/download`、`/index.csv`）只 SELECT 当前活动版本 `index_entries`；实测与 SQLite 逐行 MATCH | ✅ |
| 14 | 可启动的 Docker Compose | `Dockerfile`（python:3.11-slim + gunicorn + healthcheck + /data 卷）与 `docker-compose.yml`（端口 8000、命名卷、healthcheck、重启策略） | ⚠️ 文件已交付并静态校验，本环境无 docker 守护进程，未实机构建（见第三节） |
| 15 | 交付前核对改动、网页试跑补录/撤下/下载并写明结果 | `docs/pytest-output.txt`、`docs/web-trial-transcript.txt`、本文件；`git status` 已核对 | ✅ |

---

## 二、实测结果（均为本环境真实执行）

### 1. 自动化测试：20/20 通过

命令：`python3 -m pytest`（Flask 3.1.3 / pytest 9.2.0 / Python 3.11.2）

```
20 passed in 0.65s
```

覆盖：CSV/名单解析 9 例（分隔符、引号逗号、宽表、无表头、同号去重、空号报错、GBK、撤下名单注释）、
图逻辑 6 例（缺件的直接/传递暂缓、互引环、环上缺件、撤下传播+补录恢复、
同号整条替换、增量 scope 的旧边/新边/无向性、**失败事务回滚且旧索引不变**）、
Web 全流程 4 例（空首页、上传→查看→下载→撤下→补录恢复、坏 CSV 422 且版本不变、
多号文本框撤下）。完整清单见 `docs/pytest-output.txt`。

### 2. 真实 gunicorn + curl 网页试跑

用与容器相同的 gunicorn（1 worker / 4 线程）在 `127.0.0.1:8767` 起真实 HTTP 服务，
独立临时 SQLite 库，完整记录见 `docs/web-trial-transcript.txt`。关键结果：

**① 首批 5 行入库（A3 引用尚不存在的 A4）**

```
v2 stats={total:5, serving:3, suspended:2, missing:1}
serving   = [A2, B1, B2]
suspended = [A1, A3]          # A1→A3→A4，A1 为间接引用缺件
missing   = [A4]
scope     = [A1,A2,A3,A4,B1,B2]   # 连通闭包把缺件节点 A4 也圈入
```

页面 200，成功 flash、供数/暂缓/缺件三个表、断链路径、scope 面板、撤下按钮均渲染；
渲染后 HTML 标签配对检查无错误。

**② 坏 CSV（图版号为空）上传**

```
POST /upload(bad.csv) -> HTTP 422，页面显示「CSV 校验未通过，整批未入库」
活动索引 before=v2, after=v2      # 原索引未受影响
```

**③ 网页「补录」A4**

```
stats={total:6, serving:6, suspended:0, missing:0}   # A3、A1 自动恢复
```

**④ 网页行内「撤下」A3（POST /withdraw number=A3）**

```
HTTP 200；已撤下 1 个号…发布索引 v4，可供 4 件
serving=[A2,A4,B1,B2]  suspended=[A1]  missing=[A3]
scope=[A1,A2,A3,A4]（4 节点，与 B1/B2 无关，未重算 B 组）
```

**⑤ multipart 文件再次补录 A3（修订题名）→ 自动恢复**

```
stats={total:6, serving:6, suspended:0, missing:0}
```

**⑥ 下载与已发布索引一致性（直接读 index_entries 活动版本）**

```
download1.csv vs index v2: MATCH (3 rows)
download2.csv vs index v4: MATCH (4 rows)
download3.csv vs index v5: MATCH (6 rows)
ALL CSV/INDEX CONSISTENCY CHECKS PASSED
```

响应头：`Content-Type: text/csv; charset=utf-8`、
`Content-Disposition: attachment; filename=published_index_v4.csv`、
`X-Index-Version: 4`，文件带 UTF-8 BOM（Excel 打开中文不乱码）。

**⑦ 页面/接口探活**：`/`、`/upload`、`/plate?n=A1`、`/plate?n=A4`、`/healthz` 均 200；
默认 `DB_PATH=./data/catalog.db` 冷启动建库建索引也已实测。

### 3. 「失败仍供原索引」的验证方式

- 自动化：`test_failed_apply_rolls_back_plates_and_keeps_old_index` 在写入后、
  发布前注入异常，断言活动版本号不变、旧版本 `index_entries` 内容逐字段不变、
  未提交的 BAD 图版被回滚，且同连接随后仍可正常入库。
- HTTP 面：坏 CSV 实测 422 且活动版本保持 v2。

### 4. 交付前改动核对

- `find`/`git status` 已核对：新增 `app/`（5 个 Python 模块 + 4 模板 + 静态资源）、
  `tests/`、`docs/`、`Dockerfile`、`docker-compose.yml`、`requirements.txt`、
  `wsgi.py`、`run.py`、`README.md`、`DELIVERY.md`。无多余数据文件入库
  （`data/`、`*.db*`、`.venv`、缓存均在 `.gitignore`/`.dockerignore`）。
- `docker-compose.yml` 已用 PyYAML 解析：服务名 `catalog`、`build: .`、
  `8000:8000`、命名卷 `catalog-data:/data`、healthcheck 命令完整。

---

## 三、未完成 / 受限事项（如实说明）

1. **未在本环境实跑 `docker compose up --build`**：该环境没有 `docker`/`podman`
   可执行文件，也没有可用守护进程与 root 权限，无法构建镜像。已完成的替代验证：
   Compose YAML 解析通过、Dockerfile 指令与启动命令人工核对、用容器同款
   `gunicorn wsgi:app` 在本机真实起服务并跑通上述全部 HTTP 流程。
   **建议在有 Docker 的机器上执行一次 `docker compose up --build` 做最终确认。**
2. **未做像素级浏览器截图**：环境无 Chromium，且缺少其系统共享库、无 root 安装。
   已用真实 HTTP 请求 + 渲染 HTML 的关键内容断言与标签配对检查替代；CSS 为手写
   响应式样式，未经过真实浏览器视觉回归。
3. **未加登录鉴权 / CSRF 防护**：按「合作馆内网编目工具」定位，当前任何能访问
   8000 端口的人都可上传/撤下。若跨网段或公网部署，需补认证与 CSRF。
4. **水平扩展限制**：进程内写锁 + 单 gunicorn worker 适合馆级编目量级；多副本部署
   前应改为单写者服务或迁移到 C/S 数据库。未做十万行以上的性能/分页/全文搜索
   （scope 遍历与缺件查询均走索引，但前端目前一次全量渲染）。
5. **匹配规则约定**：图版号精确匹配、区分大小写、去除首尾空白；引用号中的空白
   被视为分隔符，即图版号本身不能含空格。如业务号规则不同需调整 `split_refs`。
6. **Git 未提交**：按助手规约「仅在用户明确要求时提交」，所有成果目前是工作区
   未跟踪/新增文件；需要的话我可以再建分支提交。

---

## 四、最快验收路径

```bash
# 方式 A：Docker
docker compose up --build            # http://localhost:8000
#   可先点「载入内置演示数据」，或依次上传 samples/01、02、03 并试撤下与下载

# 方式 B：本机
python3 -m pip install -r requirements.txt
python3 wsgi.py                      # http://127.0.0.1:8000
python3 -m pytest                    # 回归 20 项
```

可直接使用 `samples/` 中的剧本文件：`01_batch_with_gap.csv`（首批含缺件）→
`02_fill_missing_A4.csv`（补录 A4）→ 用 `withdraw_numbers.txt` 的内容撤下 A3 →
`03_refill_A3.csv`（补回 A3）。建议的网页 3 分钟剧本：上传含缺件的批次 → 看 A1/A3
暂缓与断链链 → 上传缺件行（补录）看自动恢复 → 撤下中间件号看上游再次暂缓 →
下载 CSV 并与页面供数表核对 → 重新补录该号看恢复。本次已按此剧本实测通过。
