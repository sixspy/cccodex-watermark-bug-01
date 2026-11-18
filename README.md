# cc-watermark-lab · watermark-bug-01

CC_Codex 二期（26.11）**数据工程**产品线：**血缘水印台**（BlackSheep + 原生 JS + SQLite）。

## 一句话骨架

**产出表水印模板 → 批次盖印 → 下游追索 → 缺口对账**，全链只认**一份现行模板指针**。

## 领域

- **所属领域**：数据工程
- **子方向**：数据血缘与水印（产出表盖印 / 追索闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、BlackSheep、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，底栏），雾灰底 + 青柠强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 单一指针工程约定

| 约定 | 落点 |
|------|------|
| 指针唯一键 | `meta.tpl_ptr`（旧的 `tpl_ptr_list` / `tpl_ptr_stamp` 已废弃） |
| 指针唯一读取器 | `app/services/pointers.py::read_pointer` |
| 目录与盖印同源 | 模板目录的「现行指针」标记与盖印写的列名共用同一个 `read_pointer` |
| 双指针即拦 | 多于一个指针行时，盖印 / 追索 / 对账一律 409，不猜着读 |
| 列名同源 | 盖印写 `值@列名`，列名取自唯一指针；追索、对账各自复验一遍 |
| 脏印处置命运 | **回滚重盖**：脏印进只读 `dirty_stamp_archive`，回滚后按唯一指针重盖，不再留在追索链 |
| 纠偏并发 | `BEGIN IMMEDIATE` 包住读检查写，左右值班同时点只写一行指针 |

## 主链

1. 水印模板（表名 / 列名 / 现行版本）+ 指针目录显示唯一键
2. 向运行批次盖印水印值（列名按唯一指针）
3. 按水印值追索下游批次（独立复验列名）
4. 缺口对账（已盖印 vs 应盖印，输出命运芯片）

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-watermark-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:7000/login · **admin / 123456**

底栏五个页：**指针 / 模板 / 批次 / 追索 / 缺口**。

## Docker

```powershell
docker compose up -d --build
```

端口 **7000**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`

## 复验

见 [VERIFY.md](VERIFY.md)：含可复现步骤与每处验收对应关系。
