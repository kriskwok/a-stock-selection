# A 股选股 Skill

一个基于公开、零 API Key 数据源的 Codex Skill，用于生成可追溯的 A 股选股与投研辅助报告。

## 能力

- 扫描市场热点、行业广度和涨跌停情绪
- 结合实时行情、日线趋势、均线和量价结构筛选候选股
- 引入成长估值、机构覆盖、公告、解禁、资金流、融资融券和龙虎榜复核
- 输出技术候选、综合研究排名和候选观察池
- 生成 Markdown 与响应式 HTML 报告，并记录数据来源、日期、覆盖率和降级原因

## 使用

安装依赖：

```bash
python3 -m pip install -r scripts/requirements.txt
```

运行完整流程：

```bash
python3 scripts/a_stock_selection.py --skip-image
```

可用 `--output-dir` 指定输出目录，`--max-candidates` 和 `--enrichment-limit` 控制请求规模。`--skip-image` 仅为兼容旧命令保留，当前不会生成 PNG。

## 筛选口径

默认要求总市值不低于 100 亿元、最近交易日成交额不低于 5 亿元，并优先关注 400 亿元以上公司。技术趋势完整性是发布正式排名的硬门槛；数据不足时会降级为候选观察池，不将缺失数据伪装成零值。

本项目仅用于研究辅助，不构成投资建议。
