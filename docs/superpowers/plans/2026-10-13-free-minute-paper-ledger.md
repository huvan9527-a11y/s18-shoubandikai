# 免费分钟行情模拟仓闭环实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接通免费公开分钟行情、现有 S18 撮合账本和收盘工作流，并安全处理过期的开盘提醒。

**Architecture:** `PublicData` 为报告所选股票与持仓下载并严格校验当日分钟数据，再交给现有 `paper.replay_day` 撮合。收盘扫描成功后单独运行账本入口；账户导出保持原子写入和同日幂等。实时提醒仅在有效窗口发出，过时运行以成功跳过处理。

**Tech Stack:** Python 3.11、AkShare、pandas、GitHub Actions、unittest。

**Spec:** `docs/superpowers/specs/2026-10-13-free-minute-paper-ledger-design.md`

## Global Constraints

- 模拟资金固定为 200,000 元，最多 2 个持仓槽，每日最多新增 2 只。
- 买入时段为 09:31–09:35；卖出尝试从 T+1 的 14:50–14:56 开始，未成交持仓按既有撮合规则继续处理。
- 使用 AkShare `stock_zh_a_hist_min_em` 的 1 分钟、不复权行情。接口返回成交量的单位为“手”，传给撮合器前换算为股（每手 100 股）。
- 仅接受目标交易日当天、时间完整、字段有效的分钟条目；不补造历史分钟成交，不用日线代替分钟成交。
- 对缺行情、过时日期或不完整分钟，账本不得发生部分更新。
- 只做模拟，不向券商发送订单。
- 实时开盘提醒窗口为北京时间 09:25–09:35；窗口外不得发送提醒。

## Review Focus

- 历史分钟“开盘”字段为 0：目标日不是接口当前完整日时拒绝该日数据（Task 1 测试）。
- 成交量单位错误会放大/缩小容量：逐分钟“手”转成股后才进入撮合（Task 1 测试）。
- 分钟窗口缺失或 OHLC 自相矛盾：完整性预检失败且账本字节内容不变（Task 2 测试）。
- 第一次启用会跨过账本配置的旧起始日：从首个经过验证的当天数据开始，不追溯补造历史（Task 2 测试）。
- 16:23/18:23 重复触发或手工重复运行：同一日期最多入账一次；09:35 后提醒安全跳过（Task 2、Task 3 测试）。

---

### Task 1: 获取并规范化公开分钟行情

**Files:**
- Modify: `src/data.py`
- Test: `tests/test_public_data.py`

**Interfaces:**
- Produces: `PublicData.session(date, candidates, positions, cfg) -> (daily, minutes)`，返回既有撮合器所需的数据结构。
- Produces: 一个纯规范化辅助函数，将 AkShare 列名、时间戳及“手”单位转换为撮合器使用的字段。
- Consumes: 当前报告里的候选、账户持仓以及既有 `opening_queue` 资格筛选。

- [ ] **Step 1: 写失败测试**，覆盖分钟字段规范化、每手 100 股换算、目标日期校验、缺少 09:30–09:36 或 14:50–14:57 数据、零开盘价和错误 OHLC。
- [ ] **Step 2: 运行针对性测试并确认失败**：`python -m unittest tests.test_public_data -v`。
- [ ] **Step 3: 实现单股分钟行情获取及严格规范化**；只请求合格候选和未停牌持仓，数据异常时报出股票和缺失时段，不把错误转成空行情。
- [ ] **Step 4: 重跑针对性测试并确认通过**。
- [ ] **Step 5: 提交**：`feat: add validated public minute data for paper replay`。

### Task 2: 接通单日模拟账本并防止重复/部分入账

**Files:**
- Modify: `src/run.py`
- Test: `tests/test_run.py`、必要时 `tests/test_paper.py`

**Interfaces:**
- Consumes: Task 1 的 `PublicData.session`。
- Produces: 收盘账本入口，读取已成功生成的当日信号报告，将同日候选、当日数据和持仓传给 `paper.replay_day`，再调用既有 `export`。
- Existing invariant: `paper.replay_day` 完整预检后才变更账户；重复处理已完成日期必须是 no-op。

- [ ] **Step 1: 写失败测试**，覆盖首次有效日期起账、有效买入/卖出登记、同日重复运行、旧/缺失行情拒绝、失败后账户与导出文件保持不变。
- [ ] **Step 2: 运行针对性测试并确认失败**：`python -m unittest tests.test_run -v`。
- [ ] **Step 3: 实现只处理已扫描的最新完整交易日**；首次运行以该日为账本起点，已有账本遇到缺失交易日或分钟缺口时保留原状态并给出明确状态，不将更新日期伪装成已连续处理。
- [ ] **Step 4: 更新输出**，让 `latest.json/md`、`holdings.csv`、`trades.csv`、`equity.csv` 清楚显示有效起账日、最近处理日、现金、权益、持仓、成交数及数据失败原因。
- [ ] **Step 5: 重跑针对性测试并确认通过**。
- [ ] **Step 6: 提交**：`feat: replay validated daily paper ledger`。

### Task 3: 接入 Actions、修正超时提醒并说明查看入口

**Files:**
- Modify: `.github/workflows/s18-paper.yml`
- Modify: `src/opening_push.py`
- Modify: `README.md`
- Test: `tests/test_opening_push.py`、必要时 `tests/test_run.py`

**Interfaces:**
- Workflow order: 安装依赖与现有单测 → 盘后扫描 → 当日模拟账本 → Actions 摘要 → 持久化与报告 artifact。
- Opening behavior: 当前日的 09:25–09:35 窗口内继续验证并推送；窗口外返回“已跳过”，不发送过时通知、不使安全跳过本身变成失败。
- README 把 `outputs/latest.md` 定为人读入口，并链接 JSON、持仓、成交和净值文件。

- [ ] **Step 1: 写失败测试**，覆盖 09:25/09:35 边界、窗口外跳过、跳过时没有推送；并检查主工作流在扫描后运行模拟账本。
- [ ] **Step 2: 运行针对性测试并确认失败**：`python -m unittest tests.test_opening_push -v`。
- [ ] **Step 3: 修改提醒窗口守卫与工作流步骤**，让延迟到 09:35 之后的计划任务正常跳过；收盘任务只在扫描成功后调用模拟账本，失败时保留诊断产物。
- [ ] **Step 4: 更新 README**，说明模拟成交模型、免费的分钟行情限制、错过交易日的行为及以后查看账户的直接路径。
- [ ] **Step 5: 运行完整验证**：`python -m unittest discover -s tests -v`；检查 YAML 与输出文件差异。
- [ ] **Step 6: 提交**：`fix: run validated paper ledger and skip late alerts safely`。

## Self-Review Notes

- Spec coverage: 行情获取与校验（Task 1）；撮合、账户起点、幂等、输出（Task 2）；工作流、超时提醒、查看说明（Task 3）。
- 不读取 Actions Secrets，不引入券商下单，不放宽既有撮合参数。
- 免费接口只够支持逐日运行；任何不满足完整性约束的数据都不能改变账户状态。
