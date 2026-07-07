# APlan

面向沪深 A 股（含 300/301 创业板）的可审计量化投研框架。APlan
尝试把行情、公告、基本面、策略模型、风控、纸面模拟和审计链串成一个可复盘的研究闭环。

当前定位：**ShuangqiaoQuant 公开量化研究实验室**。项目仍处于 `research_only`
阶段，尚无通过隔离验证并获批进入模拟盘的正式策略。

项目整体结构见 [架构简介](docs/architecture.md)。如果需要给新用户、合作者或潜在投资人介绍项目，可先阅读 [项目介绍](docs/project_intro.md)。
对外发布路线见 [公开路线图](docs/public_roadmap.md)。GitHub Pages 首页位于
[docs/index.html](docs/index.html)。

> 本项目只用于研究，不构成投资建议，也不保证收益。实盘前必须完成样本外验证和模拟盘。
> 更完整的风险边界见 [DISCLAIMER.md](DISCLAIMER.md)。

## 为什么公开

APlan 不希望把 Agent 包装成“自动荐股机器”。它更适合公开展示一个严肃投研系统如何：

- 保留数据截止时间和原始快照；
- 把公告、基本面和市场环境转成可验证证据；
- 用训练集、隔离验证集和多调仓起点拒绝不稳健策略；
- 在策略、风控和审批三重闸门通过前阻止执行；
- 把失败、阻断和日报写入可审计记录。

公开项目的目标是吸引技术反馈、研究反馈和潜在合作，而不是发布交易建议。

## 发布与合规边界

- 可以公开：代码框架、测试、架构文档、脱敏示例报告、研究日志。
- 不应公开：`.env`、API Token、券商账号、个人持仓细节、受限数据源原始数据。
- 不应承诺：收益率、胜率、目标价、买卖建议、个性化投资建议。
- 如果未来提供收费研究、组合建议、代客管理或投资顾问服务，应先完成独立法律与合规审查。

## 设计边界

- Agent 负责整理公告、解释证据和生成报告，不直接修改历史数据或回测结果。
- 信号在收盘后形成，最早下一可交易日开盘执行。
- 回测模拟涨停不可买、跌停不可卖、停牌、佣金、印花税和滑点。
- 短线、波段、中期分别建模，不混用标签。
- 所有输入均保留数据截止时间，避免未来数据泄漏。

## 快速开始

项目核心仅依赖 Python 3.11+：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e .
python3 -m unittest discover -s tests -v
```

### 配置 Tushare

复制 `.env.example` 为 `.env`，填入自己的 Token；`.env` 已被版本控制忽略：

```bash
cp .env.example .env
chmod 600 .env
```

验证连接并同步上市、暂停上市和退市证券主数据：

```bash
aplan-sync verify
aplan-sync master --status L
aplan-sync master --status P  # 调用间隔以账户权限提示为准
aplan-sync master --status D
```

每次同步都会把带下载时间的原始 JSON 快照保存到
`data/raw/tushare/<日期>/`，清洗后的证券表保存到
`data/processed/tushare_stock_basic.csv`。

同步一个交易日的全部基础行情：

```bash
aplan-sync market-day --date 20260702
```

若账户权限或频率较低，可以拆开同步，已有快照会被复用：

```bash
aplan-sync market-day --date 20260702 --datasets daily,stk_limit
aplan-sync market-day --date 20260702 --datasets adj_factor
aplan-sync market-day --date 20260702 --datasets daily_basic
aplan-sync market-day --date 20260702 --datasets suspend_d
```

同步器会逐接口容错：某个附加接口无权限不会丢弃已经下载的数据。若缺少
`stk_limit`，系统仅以涨跌方向一致的一字板作为不可成交替代；这是原型降级方案，
正式回测仍应补齐准确涨跌停价。

### 历史回填

按交易日历增量回填，已经存在的原始快照会自动跳过：

```bash
# 先用 10 个交易日试跑
aplan-sync backfill \
  --start 20230101 \
  --end 20260702 \
  --datasets daily,adj_factor \
  --max-days 10

# 确认权限和频率后继续；可重复运行
aplan-sync backfill \
  --start 20230101 \
  --end 20260702 \
  --datasets daily \
  --calendar-mode weekdays \
  --workers 2 \
  --delay 2.6
```

交易日历会缓存到 `data/raw/tushare/calendars/`；账户限频恢复后只需成功获取一次。
若交易日历临时限频，可加 `--calendar-mode weekdays`。此模式会查询所有工作日，
法定休市日返回零行，因此请求数略多，但不会制造交易数据。

若已经有本地日线历史、只想补估值等附加证据，优先使用本地日线日期，避免浪费请求在
休市日。低频账户可小批量、断点续跑；`--delay` 必须按账户接口频率设置。若
`daily_basic` 返回 1 次/小时限制，则每批建议只跑 1 天，并把间隔设为 3700 秒以上：

```bash
aplan-sync backfill \
  --start 20230101 \
  --end 20260702 \
  --datasets daily_basic \
  --calendar-mode local-daily \
  --max-days 1 \
  --delay 3700
```

补完一批后重建覆盖报告：

```bash
aplan-evidence-coverage
```

### 可选：AkShare 免费数据源

如果 Tushare 限频或成本较高，可以把 AkShare 作为补充源。它不会替代 Tushare 主数据，
而是写入独立目录，便于审计和交叉验证：

```bash
python3 -m pip install -e '.[akshare]'
aplan-akshare securities --as-of 2026-07-06
aplan-akshare spot-valuations --date 20260706 --retries 2 --retry-delay 2
aplan-akshare financial-indicators --symbols 600000,000001 --as-of 2026-07-06 --start-year 2024
```

输出：

```text
data/raw/akshare/<日期>/stock_zh_a_spot_em.json
data/raw/akshare/<日期>/stock_info_securities.json
data/processed/securities.csv
data/processed/akshare_valuations/<日期>.csv
data/raw/akshare/<日期>/stock_financial_analysis_indicator/<股票代码>.json
data/processed/akshare_fundamentals/<日期>.csv
```

注意：AkShare 多数接口来自公开网页数据，字段和稳定性可能随源站变化。进入策略评分前，
应先与 Tushare 或其他来源做覆盖率、单位和字段一致性检查。
如果上游网页接口临时断开，命令会重试并输出简短错误；保留现有 Tushare/本地数据作为主流程，
不要让单一免费源的短暂失败影响策略评分。
其中 `financial-indicators` 使用下载观察时间作为 `publish_time`，适合从今天开始的日常证据补全；
没有公告发布时间校验前，不用于历史回测质量加分。

### 可选：银河星耀数智

如果你拿到了中国银河证券的星耀数智试用账号，可以作为受控付费数据源补行情、证券信息和部分历史快照。
SDK 和账号材料不应提交到公开仓库；请按数据供应方授权在本地安装。该 SDK 通常只支持 Linux/Windows x64，
在 macOS arm64 环境里不能直接运行。要在支持的机器上用它，先配置以下环境变量：

```bash
YINHE_SERVER_VIP=<your_server_host>
YINHE_SERVER_PORT=<your_server_port>
YINHE_USERNAME=<your_username>
YINHE_PASSWORD=***
YINHE_API_MODE=internet
```

然后安装 wheel 并运行对应同步命令：

```bash
python3 -m pip install /path/to/tgw-*.whl
aplan-yinhe securities --as-of 2026-07-06
aplan-yinhe daily --date 20260706 --symbols 600000,000001
```

银河数据更适合做受控补源和交叉验证，落地时仍建议先核对字段单位，再接入 `data/raw/yinhe/` 和
`data/processed/` 的标准化流程。

### 第一轮基线回测

```bash
aplan-backtest
```

结果写入 `reports/backtest_v1/`。该回测以 `pct_chg` 构造除权连续收益，以原始开盘价
模拟成交，并分别运行 10、40、120 个交易日周期。

第二轮对40日策略使用四个不同调仓起点，并与沪深300比较：

```bash
aplan-validate
```

多周期隔离验证（2023–2024选择，2025–2026验证）：

```bash
aplan-horizons
```

### 每日研究工作流

下载指定交易日、执行数据质量闸门并写入不可覆盖的审计记录：

```bash
aplan-daily --date 20260706
```

只检查已经存在的本地快照：

```bash
aplan-daily --date 20260702 --no-download
```

审计记录写入 `runs/daily/<交易日>/`。在策略通过隔离验证前，工作流固定为
`research_only`，策略阶段会被阻止，不生成交易建议。

每次运行同时生成 `reports/daily/<交易日>/` 下的 Markdown 报告。审计文件通过
前序文件 SHA-256 串成哈希链，可检查历史记录是否被修改：

```bash
aplan-audit latest
aplan-audit verify
```

### 策略插件与统一信号

策略插件实现 `StrategyPlugin` 协议，并输出 `UnifiedSignal`。信号强制包含：

- 策略ID和版本；
- 数据日期和输入文件哈希；
- 意图、持有周期、评分、置信度和目标仓位；
- 支持证据、风险及失效条件；
- 确定性的信号ID和生成时间。

查看当前注册策略：

```bash
aplan-strategies list
```

研究状态策略的信号永远不可执行。只有状态为 `validated`、完成模拟盘审批且工作流
显式开启模拟执行后，信号才可能标记为 `actionable`；实盘还需要独立审批。

### 组合与风控

初始化纸面组合必须显式提供本金：

```bash
aplan-portfolio init --id paper-main --capital 1000000 --date 20260706
aplan-portfolio show --id paper-main
```

默认风险政策包括：单股10%、最多10只、已知行业30%、总仓位95%、现金至少5%、
每日换手30%、组合回撤15%熔断及A股100股整手。策略只提出目标仓位，风控生成
订单计划；`research_only` 工作流永远不会提交订单。

### 纸面模拟成交

纸面引擎模拟下一交易日开盘、双边滑点、最低5元佣金、卖出印花税、一字板、现金
不足及T+1可卖数量。执行会修改纸面组合状态，因此需要显式确认：

```bash
aplan-paper execute \
  --portfolio paper-main \
  --date 20260707 \
  --orders /path/to/approved-paper-orders.json \
  --confirm-paper-execution
```

该命令只操作本地纸面账本，没有真实券商连接。当前没有合格策略，也未初始化纸面
本金，所以每日工作流中的模拟阶段保持 `blocked`。

### 巨潮公告与资讯Agent

同步法定披露平台的公告元数据并生成标题级结构化事件：

```bash
aplan-announcements sync --date 20260706
aplan-announcements summary --date 20260706
```

原始响应保存在 `data/raw/cninfo/<日期>/`，标准化公告和事件保存在
`data/processed/announcements/`。标题分类器会识别退市、监管、减持、诉讼、业绩、
回购、增持、重大合同、重组、停复牌和解禁等事件，但所有结果均标记为需要全文
核验，不直接生成交易信号。

下载重点公告PDF并提取全文（需要 `pypdf`）：

```bash
aplan-fulltext process --date 20260706 --risk critical,high --limit 10
```

PDF、文本和分析结果分别保存于 `data/raw/cninfo/<日期>/pdfs/`、
`data/processed/announcement_text/` 和 `data/processed/announcement_analysis/`。
全文规则分析只生成事实、正反证据和不确定性，不生成可执行信号；文本过少的扫描件
会标记为 `needs_ocr`。

### 邮件通知

使用Mac“邮件”App中已配置的发件账户发送，不在项目中保存邮箱密码：

```bash
export APLAN_EMAIL_RECIPIENT=research@example.com
export APLAN_EMAIL_SENDER=sender@example.com
aplan-email test
aplan-email send-latest
```

邮件包含数据质量、公告、高风险事件、全文处理、策略和组合摘要，并附上对应的
Markdown日报。相同审计记录默认只发送一次，发送状态保存在本地
`state/notifications/email.json`。

准备符合 [data/README.md](data/README.md) 格式的两个 CSV 后：

```bash
aplan \
  --bars data/processed/daily_bars.csv \
  --securities data/processed/securities.csv \
  --date 2026-07-02 \
  --horizon swing \
  --top 10 \
  --output reports/2026-07-02-swing.md
```

如果希望“先选候选池，再只对候选股自动补 AkShare 基本面证据”，使用研究编排命令：

```bash
aplan-research \
  --bars data/processed/daily \
  --securities data/processed/securities.csv \
  --date 2026-07-06 \
  --horizon swing \
  --top 10 \
  --auto-akshare-fundamentals \
  --akshare-start-year 2024 \
  --output reports/2026-07-06-swing.md \
  --evidence-output reports/2026-07-06-evidence.json
```

该命令会先用现有量价/估值/公告证据生成候选池，再只对候选池逐股补基本面。
AkShare 财务指标当前只做展示和风险提示，不做基本面质量加分。

生成研究候选后，可以登记到观察账本。观察账本不代表买入，只用于后续跟踪
候选股在 5/20/60 个交易日后的表现，以及记录人工反馈：

```bash
aplan-observe register \
  --bars data/processed/daily \
  --securities data/processed/securities.csv \
  --date 2026-07-06 \
  --horizon swing \
  --top 10 \
  --valuations data/processed/valuations/20260706.csv \
  --fundamentals data/processed/akshare_fundamentals/20260706.csv

aplan-observe update \
  --bars data/processed/daily \
  --date 2026-08-06 \
  --horizons 5,20,60

aplan-observe feedback \
  --id <observation_id> \
  --action bought \
  --realized-return 0.032 \
  --note "模拟买入，按突破延续观察"
```

观察记录保存在 `state/observations/observations.json`，用于后续策略复盘和规则迭代。
人工持仓风险也可以登记为观察样本，例如全仓深套、未按系统候选买入、止损纪律缺失等；
这类样本用于改进风控，不会自动生成真实交易指令。

可以生成观察复盘报告：

```bash
aplan-observe review --output reports/observations/review_20260707.md
```

复盘会按决策分层和买入风格统计样本数、平均收益和胜率。样本不足时只作观察，
不得直接改动策略权重。

因子进入评分前应先做分层验证：

```bash
aplan-factor-lab \
  --bars data/processed/daily \
  --date 2026-07-02 \
  --factor momentum20 \
  --horizon-days 20 \
  --output reports/factor_lab/momentum20_h20_20260702.json
```

首轮本地验证显示，裸 `momentum20`、`momentum60`、`turnover_trend20` 和 `low_vol20`
在未来 20 日维度暂未表现出稳定正向 top-bottom spread；它们可以作为解释和组合特征，
但不能未经验证直接升级为独立买入规则。

每日研究报告采用两层结构：

- 资格层：市场环境、行业弱势、公告/基本面风险、证据缺口和买入风格清单；
- 排序层：相对强度、趋势回撤、流动性、估值风险等分项评分。

分数高只代表优先研究，不代表可以模拟或实盘买入。候选必须通过“模拟买入前检查”。

## 当前基线策略

候选股票先通过以下过滤：

- 仅沪深主板及创业板常见代码段；
- 排除 ST、退市风险、上市不足 120 日；
- 最近 20 个交易日日均成交额不少于 5,000 万元。

横截面评分暂由 20/5/60 日动量、低波动和成交额趋势构成。这只是用来验证工程链路的基线，不应直接实盘。

配置草案位于 `config/strategy.toml`。目前 CLI 使用内置默认值，下一阶段会让配置完全驱动运行。

## 下一阶段

1. 接入带授权的日线与证券主数据，并记录数据版本。
2. 建立交易日历、退市股票和历史 ST 状态，消除幸存者偏差。
3. 加入财务因子，严格按实际披露时间对齐。
4. 建立组合级回测、行业约束、基准和最大回撤指标。
5. 接入巨潮公告 Agent，输出带原文引用的正反证据。
6. 连续运行模拟盘后，才考虑人工确认的小资金实盘。
