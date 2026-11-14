# 离线退回核销区间分析协议（recall-writeoff-interval/1）

本层只读复用已构建的召回影响集市（`recall-impact-mart/1`）与一份退回 CSV，
在**全部可行核销分配**上求指定指标的精确实数最小/最大值及完整分配见证。
它不抓站点、不重建集市、不修改集市或 CSV 的任何字节；输入非法时整次拒绝，
已存在的集市与报告文件保持不变。

## 1. 输入

### 影响集市（`--mart`）

以 `file:...?mode=ro` 只读 URI 打开，必须含 `meta`、`recalls`、`shipments`、
`shipment_recall_impact` 四张表且所需列齐备，`meta.format` 必须等于
`recall-impact-mart/1`。四张表在同一只读事务中读取，共享一个快照。

- `shipments.units` 必须是 ASCII 十进制整数串，范围 1..1000000000。
- `shipment_recall_impact` 的每一行必须引用已存在的发货与公告。
- 任意结构或取值错误都使整次分析失败；本层绝不 DDL/DML 源集市。

### 退回 CSV（`--returns`，UTF-8，固定有序表头）

```
return_id,customer_id,units,candidate_shipment_ids
```

* 每份文件一次性读取原始字节；CSV 解析与报告中的 SHA-256 使用同一份字节。
* `return_id` 全局唯一；`units` 为正整数（ASCII 数字，1..1000000000）。
* `candidate_shipment_ids` 是非空的 JSON 字符串数组，至少 1 个、不得重复。
* 每个候选发货必须存在于集市的 `shipments`，且其 `customer_id` 与退回行的
  `customer_id` **精确相等**（同客户核销）。
* 标识按原文精确比较：不去空白、不改大小写、不补零；空值或全空白值拒绝。
* 一条物理行一条记录；带引号字段内的换行显式拒绝；空白分隔行不计记录号。
* 记录位置是从 1 开始的数据记录序号，表头不计。

## 2. 核销模型

设退回 `i` 的件数为 `d_i`（整数），候选发货集合为 `C_i`；发货 `j` 的原始
`units` 为容量 `cap_j`。一个**分配**给每个 `(i, j∈C_i)` 赋非负整数 `x_{ij}`，
满足：

1. **逐笔分完**：对每个退回 `i`，`sum_{j∈C_i} x_{ij} = d_i`（整数拆分，不限
   单笔拆成几份）；
2. **发货不超容**：对每个发货 `j`，`sum_{i: j∈C_i} x_{ij} ≤ cap_j`。

候选可混合受影响发货与仅 `not_linked_by_supplied_data` 的发货；所有退回共享
同一批发货容量，因此不同退回行相互制约。逐行贪心（例如每行优先塞第一个候选）
**不是**有效算法：一行先占用的容量可能使另一行无解或错过极值。

## 3. 指标与区间

对发货集合 `T`，记核销到 `T` 的件数

```
value(T) = sum_{j∈T} sum_i x_{ij}
```

在**全部满足 1、2 的整数分配**上，本层报告：

* `affected_union`：`T` = 被集市中至少一个公告关联的发货并集（交集限定在本次
  退回的候选发货内）。每个发货只计一次；不同公告的重叠发货不重复累加。
* `recalls[recall_id]`：`T` = 被该 `recall_id` 关联的发货集合。

对每个指标分别独立求精确最小 `min` 与最大 `max`（各自可能对应不同分配），
并给出该端点的**完整整数分配见证**。指标之间不得相加。

### 精确区间的算法（不枚举方案、不按件展开）

可行性、与两个端点都由整数最大流决定，容量为任意精度整数，每次增广推送整条
路径的瓶颈，十亿件容量也只走常量级增广次数。

- `max = maxflow(仅允许 T 内发货)`：把退回需求经候选边路由到 `T`。
- `min = 总需求 − maxflow(仅允许 T 的补集发货)`：先让补集尽量多承担，剩余
  必然落在 `T`。该恒等式对任意线性 0/1 指标成立，且两端都是可达值。
- 见证：在一个带下界的环流网络上，把「每笔分完」「发货不超容」「`T` 上总量
  恰为端点值」编码为上下界，求一次可行环流；`退回→发货` 边的流量即完整分配。

复杂度为多项式：每个指标 2 次最大流 + 2 次环流，与方案总数无关。40 笔二选一
退回有 `2^40` 量级方案空间，求解仍是多项式时间。

## 4. 无解：不可行证书

若不存在满足 1、2 的分配，`status` 为 `infeasible`，`metrics` 为 `null`，
`infeasibility` 给出一个**退回子集** `A` 与其候选发货集合 `N(A)`：

```
demanded_units  = sum_{i∈A} d_i
available_units = sum_{j∈N(A)} cap_j
shortfall_units = demanded − available > 0
```

证书取自最大流的残余可达集：候选边容量为 `F+1`（`F` 为总需求），永不饱和，
故可达的退回其全部候选发货也可达；源侧割容量 `< F` 直接导出上述严格不足。
**总容量充足但局部不足**、**各行单独可行却共同冲突**都表现为这种证书；绝不
当作「零退回」处理。

## 5. 报告（`--report` 输出，亦打印到 stdout）

`recall-writeoff-interval/1`，键排序、`ensure_ascii=False`、紧凑分隔符，稳定
可复现。主要字段：

```jsonc
{
  "format": "recall-writeoff-interval/1",
  "inputs": {"mart_format": "...", "mart_logic_snapshot_sha256": "...",
             "returns_sha256": "..."},
  "totals": {"returns": 2, "return_units": 10,
             "candidate_shipments": 3, "shipments_in_mart": 3},
  "status": "feasible" | "infeasible",
  "infeasibility": null | {"returns": [...], "candidate_shipments": [...],
                           "demanded_units": N, "available_units": M,
                           "shortfall_units": N-M},
  "metrics": null | {
    "affected_union": {"target_shipment_ids": [...],
                       "min": <witness>, "max": <witness>},
    "recalls": {"<recall_id>": {"target_shipment_ids": [...],
                                "min": <witness>, "max": <witness>}}
  }
}
```

每个 `<witness>`：

```jsonc
{"units": <该端点值>, "shipments_touched": <使用到的发货数>,
 "allocation": [{"return_id": "...", "units": d,
                 "assignments": [{"shipment_id": "...", "units": x}, ...]}, ...],
 "shipment_totals": {"<shipment_id>": <该发货合计>, ...}}
```

分配按 `return_id` 排序，`assignments` 与 `shipment_totals` 的键按发货 ID
排序，故与 CSV 行序、JSON 数组顺序无关。

## 6. 失败与原子性

* 任何输入错误（缺文件、集市结构不兼容、CSV 表头/取值/JSON 非法、候选不存在
  或跨客户、重复 ID 等）在产出任何结果前抛出 `WriteoffInputError`，进程非零
  退出并把原因写到 stderr。
* 源集市只读打开，成功或失败都不改动其字节。
* `--report` 先写入同目录临时文件，`os.replace` 原子替换；失败时清理临时文件，
  已存在的报告保持原样。`--report` 不得为符号链接，也不得按词法绝对路径或
  设备/inode 与集市、退回 CSV 指向同一物理文件。

## 7. 边界

* 只读现有集市，不重新抓取公告、不重算谱系、不校验公告冲突规则。
* 未与任何公告关联的发货只能表述为 `not_linked_by_supplied_data`，不得当作安全。
* 全部为合成演示数据，不构成食品安全或监管判定；不发起网络请求。
