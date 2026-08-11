# 每日学术论文追踪器

这是一个通过 GitHub Actions 每天自动运行的论文追踪器：使用官方 Semantic Scholar API 按关键词检索，按来源优先级筛选，再将结构化中文阅读笔记推送到 Server 酱。

## 当前定制

- **运行频率**：每天 08:00（中国标准时间，UTC+8）；也可在 Actions 页面手动运行。
- **检索方式**：关键词匹配，不再依赖正向或负向 seed paper。
- **研究方向**：大语言模型/生成式模型水印、大模型指纹与模型溯源、LLM 账号/用户/作者识别、医学肠道内镜模型。
- **优先来源**：网络安全 CCF A → 人工智能 CCF A / EMNLP → 高影响力期刊 → 其他会议或期刊 → arXiv。
- **阅读笔记**：每天保底推送 3 篇；多关键词匹配更强时可扩展至 5 篇。每篇均包含作者、会议/期刊缩写、CCF 等级、发表时间、匹配方向以及“问题—方法—结果—局限—可复用点”。
- **模型**：通过 OpenAI 兼容端点 `https://4router.net/v1` 调用 `gpt-5.6-sol`。

## 配置

### GitHub Secrets

在仓库的 `Settings` → `Secrets and variables` → `Actions` 添加以下两个 Secret：

- `S2_API_Key`：官方 Semantic Scholar API Key。
- `LLM_API_KEY`：4Router 的 API Key。
- `SERVERCHAN_KEY`：Server 酱 SendKey。

脚本通过官方 `https://api.semanticscholar.org/graph/v1` 接口访问 Semantic Scholar，并在进程内将请求严格节流为每秒至多一次；官方服务持续不可用时自动回退到 arXiv 公开 API。

### 论文偏好

在 `config/paper_preferences.json` 中可以修改：

- `topics`：研究方向名称与对应的多条英文检索关键词。
- `venue_metadata`：会议/期刊别名、缩写、CCF 等级与来源优先级。
- `high_impact_journals`：优先收录的高影响力期刊别名。
- `search_lookback_days`：每次检索的回溯窗口，默认 30 天。
- `max_results_per_query`：单个关键词的查询量。
- `min_papers_per_digest`、`max_papers_per_digest`、`extra_paper_min_query_matches`：每日保底数量、最高数量，以及扩展到第 4–5 篇时所需的多关键词匹配数。

### 去重与黑名单

- `config/seen_papers.txt` 由工作流自动维护，已推送论文不会重复发送。
- `config/blacklisted_venues.txt` 每行一个会议或期刊名称片段；匹配到的来源会被过滤。

旧的 `seed_paper_positive.csv` 与 `seed_paper_negative.csv` 不再参与检索，可留空或删除。

## 手动运行

```bash
pip install requests openai
LLM_API_KEY=your_key SERVERCHAN_KEY=your_sendkey python3 paper_tracker.py
```
