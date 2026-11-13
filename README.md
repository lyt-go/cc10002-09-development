# 数仓分层建模与治理平台

面向原始数据落地、清洗转换、分层建模、维度事实设计、分区治理、质量校验、血缘追踪与历史补数的数据仓库工程管理平台。

这是一个前后端分离的管理平台：前端 React + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 React + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 原始落地 | `landing` | 落地批次 | 批次编号、来源系统、落地路径 |
| 贴源层 | `ods` | 贴源表 | 表编号、表名称、来源系统 |
| 明细层 | `dwd` | 明细表 | 表编号、表名称、业务域 |
| 维度层 | `dim` | 维度表 | 维度编号、维度名称、自然键列 |
| 汇总层 | `dws` | 汇总表 | 表编号、表名称、统计粒度 |
| 应用层 | `ads` | 应用表 | 表编号、表名称、业务场景 |
| 分区治理 | `partition` | 分区方案 | 方案编号、数据表名、分区字段 |
| 质量校验 | `quality` | 质量规则 | 规则编号、校验表名、目标字段 |
| 数据血缘 | `lineage` | 血缘关系 | 关系编号、上游表名、下游表名 |
| 历史补数 | `backfill` | 补数任务 | 任务编号、目标表名、开始日期 |
| 清洗规则 | `cleanrule` | 清洗规则 | 规则编号、输入表名、空值策略 |
| 转换编排 | `transform` | 转换节点 | 节点编号、输入表名、输出表名 |
| 数据字典 | `dictionary` | 字典条目 | 条目编号、表名、字段名称 |
| 元数据管理 | `metamodel` | 元数据项 | 元数据编号、对象类型、对象名称 |
| 数据生命周期 | `lifecycle` | 生命周期策略 | 策略编号、数据表名、保留天数 |
| 治理计划 | `governance` | 治理计划 | 计划编号、数据表名、预建天数 |
| 存储成本 | `cost` | 成本账单 | 账单编号、数据表名、存储大小GB |
| 同步通道 | `sync` | 同步通道 | 通道编号、来源表名、目标表名 |
| 变更审计 | `auditlog` | 变更记录 | 记录编号、操作表名、变更类型 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 治理计划（先演练后执行）

治理计划把分区方案、生命周期策略与成本账单贯通成一条治理任务链：

- **生成计划**：`POST /api/governance/generate` 由已启用的分区方案生成计划。
  同一张表存在多个启用方案、或方案与生命周期策略冲突时，按生效日期更晚且
  已启用的分区方案裁决，冲突原因保留在计划的「冲突原因」字段上。
- **演练**：`POST /api/governance/{id}/dryrun` 只给出将新建、可归档、将删除
  与仍保留的分区范围，不改动任何数据。口径：锚点 = 今天 + 预建天数；将新建
  为预建天数内的未来分区，(锚点 − 冷热阈值, 今天] 为仍保留的热分区，
  (锚点 − 保留天数, 锚点 − 冷热阈值] 为可归档的冷分区，更早的超期分区进入删除。
- **正式执行**：`POST /api/governance/{id}/execute` 逐个分区记录结果；同一计划
  同时只允许一个作业生效；中断（可用 `interrupt_after` 演示）后通过
  `POST /api/governance/{id}/resume` 从失败分区续做，已经成功的分区不重复
  迁移或删除。
- **归档证据**：归档完成证据写入台账并落盘持久化
  （`backend/data/governance_ledger.json`，可用 `GOVERNANCE_LEDGER_PATH`
  覆盖）；删除前必须查到该分区的已持久化证据，任何证据缺失都会被阻断。
  外部系统的证据可经 `POST /api/governance/evidence` 登记。
- **审计与成本联动**：每个执行结论同步写入变更审计，并保留演练与实际执行的
  差异；归档/删除成功的分区按估算口径联动成本账单，治理页与成本页通过
  `GET /api/governance/impact` 互相跳转。

领域：数据工程
子方向：数仓与数据湖
