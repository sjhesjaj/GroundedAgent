# 换货执行时扣减库存（阶段 C）

**问题**：`create_exchange` 只检查“目标 SKU 库存 ≥ 该商品数量”（Guard E-13），执行时从不扣减库存。阶段 B 的场景 3 里，库存 5 件的 SKU 被 20 个换货同时申请，加锁和不加锁的结果一样：每轮 20 个全部被接受，超卖 15 件。

**决策 C-D1（只适用于产品运行时，在产品里取代 S6-D10）**：产品运行时执行换货时扣减库存。Stage 6 及其所有评测（eval_v2、M3 评测里的 Stage 6 部分、Stage 6 oracle）继续遵守 S6-D10，即“换货不扣减库存，库存只是前置条件”，所以已经冻结的标签、评分规则和正式结果都不受影响，DEV oracle 仍是 40/40。实现方式：给 `ActionGateway` 加一个开关 `deduct_exchange_stock`，默认关闭；只有 `aftersales_service/demo_store.py` 打开它，评测代码里不出现这个开关（有测试固定这一点）。

**在哪个事务里扣**：在执行换货的同一个事务里扣，位置是 `ActionGateway._write_execution`，直接执行（ALLOW）和审批后执行（T2）都走这里。先扣库存，再写售后单和回执，三者一起提交或一起回滚。

**扣哪个 SKU、扣多少**：扣参数里的 `target_sku`，数量等于该订单项的 `quantity`，也就是 Guard 本次读到的值。

**怎么扣**：用一条带条件的 UPDATE，只有库存够、而且版本号还是 Guard 读到的那个时，才会更新：

```sql
UPDATE inventory SET available_qty = available_qty - ?, updated_at = ?, version = version + 1
WHERE sku = ? AND available_qty >= ? AND version = ?
```

这条语句放在 `action_store.py`，是这个模块里唯一一条涉及 inventory 的语句。冻结的边界测试只做了一处放宽：允许这一条语句，并且要求它和上面写的完全一致。

**加锁**：
- SQLite：靠 `BEGIN IMMEDIATE` 保证同一时刻只有一个写入方。
- PostgreSQL：目标 SKU 的库存行从 `FOR SHARE` 改为 `FOR UPDATE`。如果两个请求都先拿共享锁、再升级成写锁，会互相等待而死锁。改成 `FOR UPDATE` 后，申请同一 SKU 的换货依次排队，每个请求的 Guard 都能读到最新库存。

**库存不足时返回什么**：复用现有的结果码 `inventory_unavailable`（DENIED），没有新增结果码。
- 正常情况下，Guard 判定时就会拒绝。
- 如果条件 UPDATE 一行都没更新到（库存或版本号在判定之后变了），整个事务回滚，同样返回 DENIED `inventory_unavailable`，售后单、回执都不落库。走审批的动作，待审批记录会从 APPROVED 变为 DENIED。

**审计里记什么**：不新增审计事件名（事件名清单是冻结的）。
- 扣减成功：照常记 `guard.evaluated` 和 `action.executed`。回执的 snapshot 里记有判定时的库存值和版本号，库存行本身的 `version` 加 1。
- 扣减失败：记一条 `action.not_executed`，decision 为 DENIED，code 为 `inventory_unavailable`。

**结果**（PostgreSQL，20 线程 × 20 轮，初始库存 5）：

| | 每轮接受 | 超卖 | 最终库存 |
|---|---|---|---|
| 修复前（加不加锁都一样） | 20 | 15 件 | 5 |
| 修复后，加锁 | 5 | 0 件 | 0 |

**后续事项（本阶段没做）**：换货被取消或驳回后把库存还回去；补货；库存预警。
