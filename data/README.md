# 数据约定

`raw/` 保存供应商原始快照，`processed/` 保存清洗后的研究数据。原始快照不可覆盖。

日线 CSV 必需列：

```text
symbol,trade_date,open,high,low,close,volume,turnover,is_suspended,is_limit_up,is_limit_down
```

估值 CSV 可选列：

```text
symbol,trade_date,pe,pb,total_mv,circ_mv,turnover_rate,volume_ratio
```

基本面 CSV 可选列：

```text
symbol,period_end,publish_time,source,source_hash,revenue_growth,net_profit_growth,roe,operating_cashflow_to_profit,debt_to_assets
```

公告事件 JSON 可选来源：

```text
data/processed/announcements/YYYYMMDD.json
data/processed/announcement_analysis/YYYYMMDD.json
```

股票信息 CSV 必需列：

```text
symbol,name,list_date,industry,is_st,is_delisting_risk
```

- `symbol` 使用六位代码，不附交易所后缀。
- `trade_date/list_date` 使用 `YYYY-MM-DD`。
- 行情价格须采用同一种复权口径，并记录数据供应商和下载时间。
- 财务与公告数据后续必须同时保存 `period_end` 和 `publish_time`；回测只能按 `publish_time` 可见。
- `daily_basic` 估值快照属于交易日可见的横截面数据；负 PE/PB 不得解释为低估。
- 基本面数据必须同时保留 `period_end` 和 `publish_time`；候选生成只能读取
  `publish_time <= 信号日收盘时间` 的快照。第一版仅展示和风险提示，不启用质量加分。
- 公告事件必须使用 `published_at` 做可见性过滤；高风险/重大风险优先作为降级或否决，
  正面公告在全文确认和价格/成交量验证前不得直接加分。
- 公告全文分析只提高证据质量和风险审阅能力；正向催化必须经事件回测和价格/成交量确认后
  才能进入加分规则。

## Tushare 单位转换

- `daily.vol` 原始单位为手，APlan 转换为股（乘以 100）。
- `daily.amount` 原始单位为千元，APlan 转换为元（乘以 1,000）。
- 涨跌停状态按开盘价是否达到 `stk_limit` 价格判断，以模拟开盘无法成交。
- 如果账户没有 `stk_limit` 权限，仅把上涨/下跌的一字板作为不可成交的保守替代；
  普通开盘封板无法可靠识别，正式回测前应补齐官方涨跌停价格。
