# data-qd-106 · 食品召回公告采集与离线影响集市

面向召回分析前的数据准备，分两个互不干扰的阶段：

1. **公告采集**：从明确的本地模拟食品公告站抓取 HTML 文件，解析产品、批号
   和召回原因，消除多页重复并保存 SQLite 来源链路。已实现页面遍历、循环链接
   保护、字段验证、内容冲突拒绝和事务落库。
2. **召回影响集市**（`recall_collect.impact`）：只读复用已接受的公告库，
   叠加工厂导出的产品映射、批次、生产谱系成员与发货四份 CSV，全量派生一个
   自包含 SQLite 集市，分析人员可直接用 SQL 查询影响范围、规范追溯依据、
   逐公告批号缺口与去重汇总。集市不重新抓站点，不修改采集库与公告冲突规则。

本地模拟站是输入来源，当前不宣称连接任何真实监管机构。集市中的谱系与影响
策略均为原创合成演示，不作实际食品安全或监管判定。

演示首轮应得到 pages=5、source_unique_recalls=2、inserted=2、stored=2；
相同数据库重跑 inserted=0。可用标准库 sqlite3 查询 recalls 与 sources 核查内容。

## 安装与运行

要求 Python 3.11 或更高；先确认 `python3 --version`。以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m recall_collect.cli --source fixtures/site --database run/recalls.sqlite
.venv/bin/python -m recall_collect.impact \
  --recalls-db run/recalls.sqlite \
  --products fixtures/factory/products.csv \
  --lots fixtures/factory/lots.csv \
  --process-members fixtures/factory/process-members.csv \
  --shipments fixtures/factory/shipments.csv \
  --output run/impact.sqlite
.venv/bin/python -m unittest discover -s tests -v
```

无 pip/venv 的精简环境可改用源码路径运行（仅依赖标准库）：

```sh
PYTHONPATH=src python3 -m recall_collect.cli --source fixtures/site --database run/recalls.sqlite
PYTHONPATH=src python3 -m recall_collect.impact --recalls-db run/recalls.sqlite \
  --products fixtures/factory/products.csv --lots fixtures/factory/lots.csv \
  --process-members fixtures/factory/process-members.csv \
  --shipments fixtures/factory/shipments.csv --output run/impact.sqlite
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

也可在 Python 中调用：`from recall_collect.impact import build_impact_mart`。
集市构建成功时打印汇总 JSON；参数错误或无效数据返回非零并把原因写入 stderr。

运行依赖与测试仅使用标准库；首次安装构建工具通常需要联网。每个仓库有独立
 `.venv`。安装的是源码的构建副本；编辑源码后须再次执行安装命令。
所有演示输出在忽略的 `run/` 中；演示可重复执行。临时目录中的测试不改变 fixture。
CLI 参数错误或无效数据返回非零；失败原因打印到 stderr。

## 目录

- `src/recall_collect/`：公告解析、业务转换、采集 CLI 与离线影响集市。
  - `impact/`：只读采集库快照、CSV 捕获与校验、无环校验与保守传播、
    SQLite 集市构建、`python -m recall_collect.impact` 入口。
- `fixtures/`：MIT 许可的合成输入；`fixtures/site` 为公告站，
  `fixtures/factory` 为四份工厂 CSV；来源声明见 DATA-SOURCES.md。
- `docs/contract.md`：公告站字段、口径、边界及失败约定。
- `docs/impact-mart.md`：集市表结构（主键/外键/事实粒度）、传播策略、
  缺口口径、规范路径选择、来源边界与失败重建约定。
- `tests/`：公告采集测试与集市测试（含端到端 CLI、受控并发快照、
  注入失败、别名拒绝、乱序与换行确定性，以及基于同步注入点的采集
  竞态复现与单捕获校验）。
- `requirements*.lock`、`pyproject.toml`：环境和构建声明。

本基线不承诺实时同步或生产调度。集市采用单写者离线全量重建，不含调度器或
增量修订协议。仅验证本机环境的事实见交付记录；本仓库不声称已经验证任何
特定 Linux 容器。所有路径均由调用参数提供，无宿主机绝对路径。
