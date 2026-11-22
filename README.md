# 巡天颜色变化核查网页

给巡天组用的核查网页：导入曝光覆盖 CSV，按网格列出「至少两晚合格 g/r 配对」，
数据员可以剔除/恢复单次曝光，系统只重核该曝光当夜覆盖的网格配对。

## 规则实现

* **曝光**以 `(观测夜, 曝光号)` 唯一标识，带一个 UTC 时刻和一个波段（g 或 r）。
  同一曝光可覆盖多个网格（`exposure_grids` 多对多），所以一次剔除可能影响多个格。
* **某格某夜可用**：该格该夜存在一对**均有效**的 g、r 曝光，UTC 时刻相差
  ≤ 30 分钟（含 30:00）。候选对不止一个时取时间最接近的一对；并列时按曝光 id
  稳定决胜，结果不会在两次重算之间漂移。
* **可用夜数 < 2** 的网格标记为「待补测」。
* **剔除/恢复**：在同一个事务里改曝光有效位，并重算它当夜覆盖到的每个网格。
  若原配对失效，自动换用同夜另一对合格的 g/r；只有一对都找不到时该格才失去这一夜。
  若被剔除的曝光本来就不是当前选中的配对成员，该夜仍可用（配对成员可能不变），
  重核结果会照实显示。

## 一致性与并发

* 所有写操作走 `BEGIN IMMEDIATE`，由 SQLite 串行化写事务；不存在「先读后改」的
  竞态窗口。
* `coverage` 是派生缓存，事务内由 `exposures` + `exposure_grids` 全量重算受影响的
  `(grid, night)`，因此曝光更正与覆盖结果一起提交、一起回滚。
* 读请求走独立连接（WAL），所以某次更正失败时，读者仍能看到此前已提交的曝光状态
  和覆盖结果。
* 两个并发更正（不同曝光）都成功时，两条 `UPDATE` 与各自的 coverage 重算都提交，
  最终状态同时反映两次改动；若其中一个失败（如曝光不存在），另一个照常提交。

## 运行

### Docker Compose（交付方式）

```bash
SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))') \
  docker compose up --build -d
# 打开 http://localhost:8080
```

数据库落在命名卷 `coverage-data` 的 `/data/coverage.sqlite3`，重启容器不丢数据。
`SECRET_KEY` 用于签名 CSRF 会话 cookie，共享部署前必须改。

### 本地直接跑（无 Docker 时）

```bash
python3 -m pip install -r requirements.txt
COVERAGE_DB=./data/coverage.sqlite3 python3 wsgi.py       # 开发服务器 :8080
# 或
COVERAGE_DB=./data/coverage.sqlite3 gunicorn --bind 0.0.0.0:8080 --workers 2 --threads 2 wsgi:app
```

### CSV 格式

```
grid,exposure,night,utc,band
G01,E1001,N2026-10-01,2026-10-01T12:00:00Z,g
G01,E1002,N2026-10-01,2026-10-01T12:10:00Z,r
```

* 表头也接受中文：`网格号,曝光号,观测夜编号,UTC时刻,波段`。
* UTC 接受 `Z`、`+08:00` 或 `YYYY-MM-DD HH:MM:SS`（无时区按 UTC 处理）。
* 同一 `(night, exposure_no)` 出现在多行表示该曝光覆盖多个网格；波段与时刻必须一致。
* 导入是**整批原子**的：任一行与已存曝光矛盾，整批拒绝、库里不变。
* 样例见 `samples/survey_coverage.csv`。

## 页面

* `/` 总览：每格可用夜数、状态徽标、每夜配对的两个曝光号与相隔秒数。
* `/grids/<网格号>` 单格详情：每夜配对结果、该格全部曝光（含覆盖的网格列表）、
  剔除/恢复按钮、本格更正记录。详情页另有「两晚颜色对比」入口和测光 CSV 上传。
* `/api/grids` JSON（给分析脚本用）；`/healthz` 容器健康检查。

## 两晚颜色对比（g−r）

1. 在单格详情页上传测光 CSV，每行：网格、目标号、观测夜、曝光号、测光版号、星等。
   * 星等必须是**有限数值**（`nan`/`inf`/非数字整行报错）；版号必须是**正整数**。
   * 前五列是键；同一键同一数值只记一次（重复行不报错、不重复入库）。
   * 同一键不同星等、曝光不存在或**未覆盖该网格** → 整批退回，已入库星等不变。
   * 一次测光版号（round）在各观测夜、各波段共用；跨夜比较必须用同一版号。
2. 选一个目标和两个不同观测夜，点「对比」。结果**只读当前已提交的数据**，并且
   每次请求都在同一读快照里重新读取 `coverage`，所以重新比对总按现行配对判定。
   * 取四次曝光（两晚各一 g、一 r）星等都齐备的**最高共同版号**；
   * 新版没齐时自动退回仍满足配对的旧版；
   * 列出两晚所用 g/r 曝光号、版号、两晚 g 星等减 r 星等，以及
     **后选夜 − 先选夜** 的色指数差；
   * 某夜没有现行 g/r 配对、或找不到共同版号时，提示「暂不可比」而不猜数。
3. 在详情页剔除配对中的曝光后，配对可能换成另一对；重新对比会按新配对重新判定，
   若新配对没有同版四行测光，就显示暂不可比。为新配对补测光后即可再次比较。

样例：`samples/photometry.csv`（含完整 v1 与不完整的 v2，应回退到 v1）、
`samples/photometry_complete_v2.csv`（补齐 v2 后应用 v2）、
`samples/photometry_alt_pair.csv`（换配对后的 v3 数据）、
`samples/photometry_conflict.csv`（同键不同值，会被退回）。

## 代码结构

```
app/db.py          schema、配对算法、事务、重核、审计、测光导入与对比
app/importer.py    CSV 解析与逐行校验（曝光覆盖 + 测光）
app/__init__.py    Flask 应用工厂与路由（CSRF 保护）
app/templates/     Jinja 模板
tests/             pytest（63 项）
scripts/           verify_multi_grid_invalidation.py、verify_colour_comparison.py、repair_coverage.py
docs/verification.md  本次交付的验证记录
```

## 维护脚本

```bash
python3 scripts/verify_multi_grid_invalidation.py        # 剔除影响多格的试跑（临时库）
python3 scripts/verify_colour_comparison.py              # 两晚对比全流程试跑（临时库）
python3 scripts/verify_multi_grid_invalidation.py --db ./data/coverage.sqlite3  # 在实库上跑（会改数据！）
python3 scripts/repair_coverage.py --db ./data/coverage.sqlite3 --check   # 只读体检
python3 scripts/repair_coverage.py --db ./data/coverage.sqlite3           # 从曝光表重建缓存
python3 -m pytest -q
```

## 遗留工作

1. **Docker 未在本机实际启动验证**（环境无 Docker daemon、无容器权限）。compose/
   Dockerfile 已按 `8080`、`/data` 卷、健康检查写好并做了 YAML 结构校验，但需要在有
   Docker 的机器上实跑一次 `docker compose up --build` 并确认卷持久化。本轮功能已用
   gunicorn 以同样命令行在真实 HTTP 上完整走通（见 `docs/verification.md`）。
2. **身份与权限**：目前只有一个隐含角色「数据员」，审计日志 actor 固定为
   `data-steward`。接入真实登录（及按人区分 actor）未做。
3. **CSV 大批量性能**：导入逐行 `SELECT` + 受影响格全量重算，当前按万级行设计；
   百万行级需要改成临时表 + 集合式配对（或分批）。
4. **「观测夜」不做时区/日出日落换算**，直接以 CSV 的 night 字段分组；若夜间划分
   规则变化，需要在上游统一。
5. **删除/改时刻**：目前只支持有效位切换；若曝光时刻本身录错，需要重新导入
   （会因冲突被拒）或手工修库，尚未提供界面。
6. 页面无分页/搜索，格数多时总览会偏长；`/api/grids` 也未分页。
