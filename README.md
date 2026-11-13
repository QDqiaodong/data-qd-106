# data-qd-106 · 食品召回公告采集

面向召回分析前的数据准备：从明确的本地模拟食品公告站抓取 HTML 文件，
解析产品、批号和召回原因，消除多页重复并保存 SQLite 来源链路。
已实现页面遍历、循环链接保护、字段验证、内容冲突拒绝和事务落库。
本地模拟站是输入来源，当前不宣称连接任何真实监管机构。

演示首轮应得到 pages=5、source_unique_recalls=2、inserted=2、stored=2；
相同数据库重跑 inserted=0。可用标准库 sqlite3 查询 recalls 与 sources 核查内容。

## 安装与运行

要求 Python 3.11 或更高；先确认 `python3 --version`。以下命令在仓库根目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m recall_collect.cli --source fixtures/site --database run/recalls.sqlite
.venv/bin/python -m unittest discover -s tests -v
```

运行依赖与测试仅使用标准库；首次安装构建工具通常需要联网。每个仓库有独立
 `.venv`。安装的是源码的构建副本；编辑源码后须再次执行安装命令。
所有演示输出在忽略的 `run/` 中；演示可重复执行。临时目录中的测试不改变 fixture。
CLI 参数错误或无效数据返回非零；失败原因打印到 stderr。

## 目录

- `src/recall_collect/`：解析、业务转换及命令行入口。
- `fixtures/`：MIT 许可的合成输入；来源声明见 DATA-SOURCES.md。
- `docs/contract.md`：字段和口径、边界及失败约定。
- `tests/`：业务结果、边界与失败测试。
- `requirements*.lock`、`pyproject.toml`：环境和构建声明。

本基线不承诺实时同步或生产调度。仅验证本机环境的事实见交付记录；本仓库不
 声称已经验证任何特定 Linux 容器。所有路径均由调用参数提供，无宿主机绝对路径。
