# 复验纸 · watermark-bug-01

主题：**库里同时挂着两份现行模板指针 → 收敛为唯一指针，且盖印/追索/对账同源。**

下面每一步都能在干净工作区里原样重跑。命令均在仓库根目录执行。

---

## 0. 准备

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest -q
```

预期：**全部通过**（基线 4 条 + 本轮 15 条回归）。

拿一份可随意破坏的库：

```bash
export DATABASE_PATH=/tmp/wm-verify.db
rm -f "$DATABASE_PATH"
python run.py          # 另开一个终端；或 PORT=7512 python run.py
```

登录：`http://127.0.0.1:7000/login`，**admin / 123456**。

---

## 1. 复现原缺陷（两份指针 + 列名漂移 + 脏印）

在另一终端直接改库，等价于缺陷版数据：

```bash
python - <<'PY'
import os, sqlite3
c = sqlite3.connect(os.environ["DATABASE_PATH"])
c.execute("DELETE FROM meta WHERE key LIKE 'tpl_ptr%'")
c.execute("INSERT INTO meta(key,value) VALUES('tpl_ptr_list','wm_batch')")   # 东指针
c.execute("INSERT INTO meta(key,value) VALUES('tpl_ptr_stamp','wm_trace')")  # 西指针
c.execute("UPDATE watermark_templates SET column_name='wm_trace' WHERE table_name='dm_gmv_d'")
c.execute("UPDATE run_batches SET status='已盖印', watermark_value='WM-DIRTY-9@wm_trace' "
          "WHERE run_code='ord-20261114-01'")
c.commit()
PY
```

### 四处顺着核

| 面 | 操作 | 未修复时应看到 | 本次实现 |
|----|------|----------------|----------|
| 指针目录 | `GET /api/pointer` | 两份指针各自生效 | `status_chip=双指针（已拦截）`，`keys` 列出两个 key |
| 盖印列表 | `POST /api/batches/1/stamp`（找一个**待盖印**批次） | 读西指针静默兜底、照样盖 | **409**，白话提示「双指针即拦…请点纠偏」 |
| 追索详情 | `POST /api/trace {"watermark_value":"WM-ORD-131"}` | 不看列名，照常返回 | **409**，白话拦截（脏印值同样 409） |
| 对账水线 | `POST /api/gap-reports {"as_of_date":"2026-11-16"}` | 落库，数字对不上 | **409**，白话拦截 |

```bash
curl -s -b c.txt http://127.0.0.1:7000/api/pointer
curl -s -b c.txt -H 'Content-Type: application/json' \
  -d '{"watermark_value":"WM-ORD-131"}' http://127.0.0.1:7000/api/trace
curl -s -b c.txt -H 'Content-Type: application/json' \
  -d '{"as_of_date":"2026-11-16"}' http://127.0.0.1:7000/api/gap-reports
```

> `c.txt` 这样拿：
> ```bash
> curl -s -c c.txt -H 'Content-Type: application/json' \
>   -d '{"username":"admin","password":"123456"}' http://127.0.0.1:7000/api/login
> ```

---

## 2. 点纠偏 → 一次写库

页面上点「指针」→ **点纠偏**，或：

```bash
curl -s -b c.txt -H 'Content-Type: application/json' \
  -d '{}' http://127.0.0.1:7000/api/pointer/correct
```

预期返回要点：

- `pointer` = 唯一指针（此例 `wm_batch`）
- `pointer_key` = `tpl_ptr`
- `fate_chip` = `脏印回滚 · 按唯一指针重盖`
- `message` 含「**已一次写库**」「脏印 1 笔已回滚并按唯一指针重盖 1 笔」
- `job` 为落档任务行

再核：

```bash
curl -s -b c.txt http://127.0.0.1:7000/api/pointer          # unique=true, 3/3 同源
curl -s -b c.txt http://127.0.0.1:7000/api/pointer/integrity # ok=true, pointers_agree=true
curl -s -b c.txt http://127.0.0.1:7000/api/pointer/jobs      # 编排任务落档
curl -s -b c.txt http://127.0.0.1:7000/api/pointer/archive   # 只读历史脏印
```

数据库里指针行应恰好一行：

```bash
python -c "import os,sqlite3;c=sqlite3.connect(os.environ['DATABASE_PATH']);\
print(c.execute(\"select key,value from meta where key like 'tpl_ptr%'\").fetchall())"
# [('tpl_ptr', 'wm_batch')]
```

---

## 3. 脏印命运：不在追索链上

所选结局：**回滚重盖**（不是「部分留存已盖脏印进只读历史并拒新盖」）。

```bash
# 旧脏值已不在追索链 → 404
curl -s -b c.txt -H 'Content-Type: application/json' \
  -d '{"watermark_value":"WM-DIRTY-9@wm_trace"}' http://127.0.0.1:7000/api/trace
# 新干净值可追索，dirty_count=0
curl -s -b c.txt -H 'Content-Type: application/json' \
  -d '{"watermark_value":"WM-DIRTY-9@wm_batch"}' http://127.0.0.1:7000/api/trace
```

只读历史仍可查：`GET /api/pointer/archive`，`old_watermark` 保留原脏值、`job_id` 指回任务。

---

## 4. 若只改目录文案却不改盖印读针 → 验收失败

这条是硬门槛。验收器 `GET /api/pointer/integrity` 不只比对两个常量，而是分别调用：

- 模板目录使用的标记逻辑 `list_templates(...)` → `catalog_pointer`
- 盖印写库使用的入口 `batches.pointer_for_stamp(...)` → `stamp_pointer`

`ok` 要求三件事同时成立：指针唯一、`catalog_pointer == stamp_pointer`、列名全部同源。

自动化复现（`tests/test_pointer_lockdown.py::test_catalog_only_fix_fails_acceptance`）把盖印读针换成旧 key，断言：

```text
unique=True, catalog_pointer=wm_batch, stamp_pointer=wm_trace,
pointers_agree=False, ok=False
```

所以「目录文案改好了」但盖印仍读另一份时，`ok` 必为 `false`。

---

## 5. 左右值班并发抢纠偏：缺锁会双写指针行

```bash
pytest -q tests/test_pointer_lockdown.py::test_concurrent_corrections_do_not_double_write_pointer
```

两个线程同时 `correct_pointer()`。断言：`meta` 里 `tpl_ptr%` 恰好一行、`== wm_batch`、`pointer_correction_jobs` 只有 1 条。

实现：整个「读指针 → 校验 → 写指针 → 对齐列名 → 处置脏印」单元包在 `BEGIN IMMEDIATE` 里（`app/db.py::write_transaction`），第二个点击排队后看到的是已收敛状态。

---

## 6. 各自复验一轮列名对齐

| 路径 | 复验点 | 代码 |
|------|--------|------|
| 盖印 | 只接受「该表现行列 == 唯一指针」，否则 409 列名漂移 | `batches._resolve_template` |
| 追索 | 自己重读指针并对源/下游所有表做列名核对，漂移即 409 | `trace._column_check` |
| 对账 | 自己重读指针，任一现行模板列名不同源即拒绝入库 | `gaps.run_gap_report` |

```bash
pytest -q tests/test_pointer_lockdown.py::test_trace_and_gap_reverify_columns_themselves
pytest -q tests/test_pointer_lockdown.py::test_dirty_stamp_is_rolled_back_not_kept_in_trace
```

---

## 7. 全量回归

```bash
pytest -q
```

预期 **19 passed**。
