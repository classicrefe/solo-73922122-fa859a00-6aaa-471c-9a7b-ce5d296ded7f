# 合作馆图版供数台（Flask + SQLite）

电子展台按图录索引中的引用号跳转。本系统负责图录分批入库、引用链完整性判定，
并维护一份**只含无断链图版的已发布供数索引**。编目员上传 CSV（图版号、题名、
引用号列表），同号整条替换，也可按号撤下；网页可查看「供数 / 暂缓 / 缺件」，
下载的 CSV 直接读取 SQLite 中的当前发布索引版本。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 容器内数据库为 `/data/catalog.db`，通过命名卷 `catalog-data` 持久化。
- 已带 `/healthz` 健康检查；对外端口 `8000:8000`。
- 可用环境变量 `SECRET_KEY` 覆盖会话密钥。

### 本地直接运行

```bash
python3 -m pip install -r requirements.txt
python3 wsgi.py            # 或 gunicorn -b 127.0.0.1:8000 wsgi:app
# http://127.0.0.1:8000
```

数据库路径可用环境变量 `DB_PATH` 覆盖，默认 `./data/catalog.db`。

### 测试

```bash
python3 -m pip install pytest
python3 -m pytest          # 20 个用例：解析 / 图算法 / 增量圈定 / 原子性 / Web 全流程
```

`samples/` 带一套 3 分钟验收剧本（含缺件批次 → 补录 → 撤下名单 → 再补回）。

## CSV 格式

表头（可识别中英文常见别名）：

```csv
图版号,题名,引用号
TP-001,彩陶纹样图版一,TP-002;TP-003
TP-002,彩陶纹样图版二,
TP-003,青铜纹饰图版,TP-002
```

- 引用号分隔符支持 `; ； | ｜ , ， 、`、空白/换行；逗号分隔时请按 CSV 规范给字段加引号。
- 也支持宽表 `图版号,题名,引用号1,引用号2,…` 与无表头按列顺序解析。
- 编码支持 UTF-8（含 BOM）与 GBK/GB18030（Excel 中文 CSV）。
- 同一文件内图版号重复时以最后一行为准并给出警告；跨批次同号 = **整条替换**。
- 任一行缺少图版号等校验错误 → 整批 422 拒绝，不写库、索引不变。

## 供数判定规则（与需求逐条对应）

把每件图版看作节点，`图版 → 引用号` 为有向边；被引用但不在 `plates` 表中的号即「缺件」。

1. **供出引用链上没有缺件的所有图版**：从所有缺件沿反向边传播，能到达缺件的
   图版一律「暂缓」，其余「供数」。
2. **齐备的互引图版一同供出**：纯互引环（如 A↔B、A→B→C→A）只要不通向缺件，
   反向缺件传播不会命中，整组同时供数。
3. **撤下连带退出 / 补录自动恢复**：撤下即删除该图版整行与其引用边，其号成为
   新的缺件根；直接或间接引用它的图版被反向传播标记为暂缓。补录该号后重新计算
   连通范围，齐备者自动回到索引。
4. **只重算相连部分**：变动号集合出发，在「变动前旧边 ∪ 本次上传的新引用边」
   构成的无向图上做闭包（出入边都走、方向不限），得到本次 scope；只对 scope 内
   的现存图版重算状态、重写其索引条目。scope 之外不存在跨边界的边，状态不变，
   发布新版本时从旧索引原样结转。
5. **改动与索引一同生效，失败仍供原索引**：图版/引用的增删、状态重算、新索引
   版本与 `active_index_version` 指针翻转处于同一个 `BEGIN IMMEDIATE` 事务；
   任何异常都 ROLLBACK，活动指针仍指向上一个完整索引，网页与下载不受影响。
   每次成功变动都会生成一个不可变的 `index_versions` 版本。
6. **下载直接读已发布索引**：`GET /download.csv`（另有 `/download`、`/index.csv`）
   只对当前活动版本的 `index_entries` 做一条 SELECT，不在下载时临时计算。

## 页面与接口

| 路径 | 说明 |
| --- | --- |
| `/` | 供数总览：统计卡、供数表、暂缓表（含断链路径）、缺件号、最近增量 scope、索引版本史、按号撤下框 |
| `/upload` | GET 上传表单 / POST 上传 CSV（`csv_file` 文件或 `csv_text` 粘贴） |
| `/withdraw` | POST 按号撤下：`number` 单个或 `numbers` 多个（换行/空格/逗号/分号分隔） |
| `/plate?n=号` | 图版/缺件详情：状态、断链 witness、直接引用与被引用清单 |
| `/demo` | POST 载入内置样例 |
| `/download.csv` `/download` `/index.csv` | 下载当前活动已发布索引（带 UTF-8 BOM，Excel 友好） |
| `/api/state` | 全量视图模型 JSON + 最近一次变动的 `changed` / `scope` |
| `/api/index` | 当前活动索引版本的条目 JSON |
| `/healthz` | 存活与活动索引版本 |

## 代码结构

```
app/
  __init__.py   # Flask 工厂、JSON/过滤器配置
  db.py         # SQLite 连接、schema（plates/refs/plate_status/index_*）
  graph.py      # CSV 解析、增量 scope 闭包、缺件反向传播
  catalog.py    # 事务化增删改 + 发布新索引版本
  routes.py     # 页面 / 下载 / JSON API
  templates/    # base/index/upload/plate 四个服务端渲染页面
  static/       # style.css 与 sample_catalog.csv
tests/          # 19 个 pytest 用例
docs/           # 实测记录（pytest 输出、Web 试跑 transcript）
Dockerfile docker-compose.yml wsgi.py
```

## 已知边界

- 面向可信编目内网，未做登录鉴权与 CSRF token（见 `DELIVERY.md`）。
- 为简化 SQLite 写串行化，Compose 使用 gunicorn 单 worker 多线程 + 进程内写锁；
  多进程/多副本部署前需换用写队列或客户端-服务器数据库。
- 图版号匹配区分大小写、去除首尾空白；引用号不允许包含空白字符（空白被视作分隔符）。
