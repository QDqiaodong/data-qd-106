# 离线召回影响集市协议（recall-impact-mart/1）

本集市是单次、离线、单写者、全量重建的派生 SQLite 文件。它只读取**已接受**
的公告采集库（只读，不执行任何建表或升级）和四份工厂 CSV，不重新抓取站点，
不改变公告冲突规则。所有内容均为原创合成演示，不构成食品安全或监管判定。

## 1. 输入约定

### 采集库（`--recalls-db`）

以 `file:...?mode=ro` 只读 URI 打开，必须含 `recalls` 与 `sources` 两表且
列序与采集器契约一致；缺库或结构不兼容直接报错。`recalls` 与 `sources`
在同一个 `BEGIN … COMMIT` 只读事务中读取，共享同一快照，采集器并发提交
无法在两表之间交错。`lots_json` 在采集器中已排序去重，集市按集合处理。

### 四份 CSV（UTF-8，固定有序表头）

| 参数 | 表头 |
| --- | --- |
| `--products` | `product_id,notice_product` |
| `--lots` | `product_id,lot_code` |
| `--process-members` | `process_id,role,product_id,lot_code` |
| `--shipments` | `shipment_id,customer_id,product_id,lot_code,units` |

* 每份文件先一次性读取原始字节；CSV 解析、落库原文与 SHA-256 使用同一份字节。
* 标识与批号按原文精确比较：不去空白、不改大小写、不补零；空值或全空白值拒绝。
* `notice_product` 与公告 `product` 文本精确匹配，不做模糊匹配。
* 记录位置是从 1 开始的**数据记录序号**，表头不计（存于 `record_position`）。
* `products` 至少一条映射；每个 `notice_product` 全局只出现一次，一个内部
  产品可有多个公告名称。`lots`、`process-members`、`shipments` 允许只有表头。
* 批次身份是 `(product_id, lot_code)` 复合键，禁止只按 `lot_code` 连接。
* `lots.product_id` 必须已在 products 声明；过程成员与发货必须引用已登记批次。
* `role` 仅接受 `input`/`output`；每个过程至少一个输入和一个输出；成员组合
  （process_id, role, product_id, lot_code）重复即拒绝，内容相同也拒绝。
* 一个批次至多由一个过程生产（多生产者整次构建失败），可被多个后续过程使用。
* `shipment_id` 唯一；`units` 为 ASCII 十进制整数字符串，范围 1..1000000000。
* 生产图必须无环，校验覆盖全部分量（含无召回命中的分量）；同一批次同时是
  某过程的输入与输出构成自环，同样拒绝。未知引用、多生产者、环路均使整次
  构建失败，不静默删边。

## 2. 影响策略（保守谱系关联）

1. 已匹配的公告批号构成**召回根**，身份为 `(recall_id, product_id, lot_code)`。
2. 根批次自身受影响，形成零跳事实。
3. 某过程的任一输入批次受根影响，则该过程的**全部输出批次**受同一根影响，
   并继续向下游传播。
4. 不按投入比例削弱影响，不计算污染概率或实际污染件数。

**缺口不是“无风险”。** 公告产品未映射记 `product_unmapped`；产品已映射但
公告批号不在 lots 中记 `lot_unregistered`；二者与 `matched` 一样逐公告批号
保存原始值。未被任何已解析根命中的发货只可表述为
`not_linked_by_supplied_data`，绝不标记为安全。

### 规范依据（canonical basis）

为每个匹配根与每个可达目标批次保存恰好一条传播事实（含根自身零跳）。
同一根的多条路径汇合不重复产生事实；不同根即使 `recall_id` 相同也分别保留。
依据选择规则：

1. 先取经过生产过程数量（跳数）最少的路径；
2. 平局时，把每一步表示为
   `(process_id, 输出 product_id, 输出 lot_code)`，按**完整步骤序列**的
   Unicode 码点字典序取最小者；不是只比最后一步，也不依赖输入行序。

事实表只存跳数、前驱批次与 `via_process`，完整路径用递归 CTE 沿前驱链
重建；规范步骤序列同时保存在逻辑快照中用于独立核对。

### 如何避免分支汇合的指数级中间结果

算法不枚举路径。对每个根做一次标签松弛：每个批次节点只持有一个当前最优
标签 `(hops, steps, predecessor, via_process)`。从根出发按生产边松弛邻接
输出；新标签仅在“跳数更少”或“同跳数但步骤序列字典序更小”时替换旧标签。
菱形或多层菱形中无论多少条路径汇合，每个目标节点始终只有一个标签、一条
事实，每个节点按改进次数有限次入队，单根复杂度为图规模的线性量级，
不会产生 2^k 条路径串，也不会用路径条数放大发货数量。

### 发货事实粒度

* `shipment_recall_impact` 的粒度是 `(shipment_id, recall_id)`：同一公告的
  多个批号、同一根的多条路径对一次发货只命中一次。
* 一次发货对应的全部根及各自规范依据通过
  `v_shipment_recall_roots`（传播事实与发货批次连接）查询。
* 按召回汇总（`v_recall_impact`）：每条发货在该召回下只计一次。
* 按客户汇总（`v_customer_impact`）：对 `shipment_id` 取并集，每条发货
  只计一次。两视图分别给出发货数与 units 合计。
* 不同召回的 units 可以重叠；**禁止**把各召回 units 合计相加当作全局
  受影响件数。客户视图的并集口径才是去重后的全局件数。

## 3. 集市表结构

所有表均带主键，外键在写入时以 `PRAGMA foreign_keys=ON` 强制。

### 元数据与来源

| 表/视图 | 主键 | 说明 |
| --- | --- | --- |
| `meta(meta_key,meta_value)` | `meta_key` | 集市格式标识 `recall-impact-mart/1`、快照种类与摘要；无时间、无机器绝对路径 |
| `logic_snapshot(snapshot_kind,canonical_json,sha256)` | `snapshot_kind` | 本次使用的逻辑事实的稳定序列化及其 SHA-256；不是源 SQLite 的二进制摘要 |
| `input_raw(input_kind,header_text,raw_bytes,sha256,source_path)` | `input_kind` | 四份 CSV 的原始字节、同份字节的摘要、表头与调用方给定路径 |
| `input_records(input_kind,record_position,raw_line)` | `(input_kind,record_position)` | 1 起数据记录序号与该记录的原始物理行文本（保留引号，去掉行结束符；空白分隔行不计号）；FK→input_raw |

规范序列化：`json.dumps(..., ensure_ascii=False, sort_keys=True,
separators=(',',':'))`，所有集合以稳定键排序后再序列化；摘要对该 UTF-8
字节流取 SHA-256。相同读取快照与相同输入字节重复构建，逻辑内容一致
（不要求 SQLite 文件二进制一致）。CSV 换行风格（LF/CRLF）变化时业务事实
与路径选择不变，但 `input_raw.sha256` 与记录位置反映真实字节/行序变化。

### 公告与映射（派生副本，支持脱机查询）

| 表 | 主键 | 外键 | 事实粒度 |
| --- | --- | --- | --- |
| `recalls` | `recall_id` | — | 每个已接受公告一行（lots 为排序去重 JSON） |
| `recall_sources` | `(recall_id,path)` | →recalls | 公告/来源路径关系，一行一条 |
| `internal_products(product_id)` | `product_id` | — | 每个内部产品一行 |
| `products(product_id,notice_product,record_position)` | `notice_product`（另设 `(product_id,notice_product)` 唯一） | →internal_products | 一条公告名称映射一行 |
| `internal_lots(product_id,lot_code,record_position)` | `(product_id,lot_code)` | →internal_products | 一个登记批次一行 |
| `processes(process_id)` | `process_id` | — | 每个过程一行 |
| `process_members(process_id,role,product_id,lot_code,record_position)` | `(process_id,role,product_id,lot_code)` | →processes, →internal_lots | 一个过程成员一行 |
| `shipments(shipment_id,customer_id,product_id,lot_code,units,record_position)` | `shipment_id` | →internal_lots | 一次发货一行，units 原文保存 |

### 解析、根、传播与影响

| 表/视图 | 主键 | 外键 | 事实粒度 |
| --- | --- | --- | --- |
| `notice_lot_resolutions(recall_id,notice_product,notice_lot,product_id,lot_code,status)` | `(recall_id,notice_product,notice_lot)` | →recalls | 每个公告批号一行；status ∈ product_unmapped / lot_unregistered / matched；缺口行 product_id 可空 |
| `recall_roots(recall_id,product_id,lot_code)` | `(recall_id,product_id,lot_code)` | →recalls, →internal_lots | 每个匹配根一行 |
| `propagation_facts(recall_id,root_product_id,root_lot_code,product_id,lot_code,hops,predecessor_product_id,predecessor_lot_code,via_process)` | `(recall_id,root_product_id,root_lot_code,product_id,lot_code)` | →recall_roots, →internal_lots | 每个（根，可达批次）一行，含零跳；前驱列为空表示根本身，via_process 为空表示零跳 |
| `shipment_recall_impact(shipment_id,recall_id,units)` | `(shipment_id,recall_id)` | →shipments, →recalls | 每发货每召回至多一行 |
| `v_shipment_link_status` | — | — | 每发货一行，linked_to_recall 或 not_linked_by_supplied_data |
| `v_shipment_recall_roots` | — | — | 发货在某召回下命中的每个根及其规范依据（跳数/前驱/via_process） |
| `v_recall_impact(recall_id,shipments_affected,units_total)` | — | — | 按召回去重的发货数与 units |
| `v_customer_impact(customer_id,shipments_affected,units_total)` | — | — | 按客户对 shipment_id 并集去重 |

## 4. 来源边界与脱机自包含

* 集市保存公告文本、来源路径关系与逻辑快照；旧 `sources` 只有路径时如实
  保留，不声称能恢复未保存的 HTML，也不证明其历史内容。
* 四份 CSV 的原始字节、SHA-256 与记录位置落库；逻辑快照中的每个事实都可
  经 `record_position` 追到对应来源记录。
* 移走采集库与四份 CSV 后，集市仍可查询全部事实、用递归 CTE 重建规范路径，
  并重新计算 `input_raw` 与 `logic_snapshot` 的摘要完成核对。

## 5. 构建、失败与重建约定

* 构建先在输出同目录的临时 SQLite 中完成全部写入、约束检查并关闭，再以
  `os.replace` 原子替换 output；任何异常都清理本次临时文件。
* 输入读取、转换、写入或替换失败均非零退出；已存在的集市保持完整，修正后
  可直接重跑（全量重建，无增量修订协议、无调度器）。
* output 不得是源数据库或任一 CSV：拒绝 output 为符号链接；按词法绝对路径
  与设备/inode 识别同路径、硬链接与符号链接别名。
* 元数据只含明确格式标识，不含当前时间或机器绝对路径；`source_path` 仅
  记录调用方给定的路径字符串，不进入逻辑快照。
