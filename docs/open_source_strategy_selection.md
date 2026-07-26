# 开源策略首轮选择记录

## 结论

首个接入对象选择 Microsoft Qlib，但不安装整套 Qlib 运行时，也不把其公开 benchmark
收益视为 APlan 可复现收益。APlan 首先实现一个隔离的研究适配器：
`qlib_alpha158_linear_lite_reference_v0_1`。

它只承担两个目标：

1. 验证“历史数据 → 特征 → 模型 → 排名 → 组合 → 成本 → 报告 → 统一研究信号”
   能够真实闭环；
2. 提供一个有公开出处的外部对照，帮助判断 APlan 自研模型的问题来自研究思想、
   数据、实现还是验证协议。

它仍处于 `research`，不会进入模拟盘或实盘，也不会打开 2025 或 2026 留出数据。

## 候选比较

| 项目 | 定位 | A股适配 | 许可证 | 首轮判断 |
|---|---|---:|---|---|
| [Microsoft Qlib](https://github.com/microsoft/qlib) | 量化研究平台、数据集、模型与组合基准 | 原生中国市场、CSI300/500 benchmark | MIT | 选用；组件边界清晰，适合做可审计外部基线 |
| [ZVT](https://github.com/zvtvz/zvt) | 数据、因子、交易与可视化平台 | 支持中国市场 | MIT | 暂缓；框架和数据耦合较重，不适合第一条最小闭环 |
| [QUANTAXIS](https://github.com/yutiansut/QUANTAXIS) | 综合量化平台 | 重点覆盖中国市场 | MIT | 暂缓；系统范围大，首轮难以隔离策略本身的贡献 |

## 采用的公开部件

- Qlib `Alpha158` 的定义来自
  [`qlib/contrib/data/loader.py`](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py)。
- 精选 20 个 Alpha158 特征来自 Qlib 的
  [`examples/benchmarks/TFT/tft.py`](https://github.com/microsoft/qlib/blob/79633dd9506ea689e5400dea0197717b5b3d74b7/examples/benchmarks/TFT/tft.py)。
- Ridge 参数 `alpha=0.05`、稳健标准化、横截面标签和 Top-k Dropout 的基础设置来自
  [`workflow_config_linear_Alpha158.yaml`](https://github.com/microsoft/qlib/blob/main/examples/benchmarks_dynamic/baseline/workflow_config_linear_Alpha158.yaml)。
- Qlib benchmark 说明与历史结果只用于确认其为真实公开研究基线，不作为 APlan
  的预期收益：
  [`examples/benchmarks/README.md`](https://github.com/microsoft/qlib/blob/main/examples/benchmarks/README.md)。

## APlan 的改动

这不是 Qlib 官方 benchmark 的原样复现，差异必须保留：

- 使用官方精选的 20 个 Alpha158 特征，而不是完整 158 个特征；
- 将这 20 个特征与官方 Ridge 线性模型组合，形成轻量适配版；
- 用 APlan 已验收的日线输入，不下载或混入 Qlib 自带数据；
- 组合回测保留 Qlib Top-k Dropout 思路，但由 APlan 实现交易成本和可交易过滤；
- 时间切分改为 APlan 冻结区间，2025 与 2026 均保持关闭；
- 所有输出均为 `WATCH` 研究信号，目标仓位为 0。

因此，任何结果只能命名为“APlan 的 Qlib 派生外部基线”，不能引用为“Qlib
官方收益复现”。

## 升级条件

只有当轻量基线完成全链路、数据审计通过，并在冻结验证段达到预注册门槛时，才允许：

1. 比较完整 Alpha158；
2. 比较 LightGBM；
3. 研究市场状态选择器或与其他模型组合。

若未通过，保留为流程对照，不搜索权重、不堆叠 rejected 模型。
