# Meridian 区域媒体证据分析服务

本服务供研究团队对本地提供的媒体观察记录进行地区、议题和受众比较。系统不抓取网页、不自动推断报道倾向，也不声称合成演示能代表真实群体。

来源登记绑定地区、媒体和允许词表；观察记录按来源和外部编号保留修订。两名研究员独立编码，分歧由第三人裁定。按地区、故事、议题和受众去重，报告显示分母、排除原因和原始证据数。

使用 `python -m meridian.api --db meridian.sqlite3` 启动 FastAPI。主要接口包括 `POST /sources`、`POST /observations`、`POST /decisions`、`POST /adjudications`、`POST /batches`、`POST /reports` 和 `POST /releases`。采集窗口使用 `/frames`，导出使用 `/reports/export` 或 `/releases/{release_id}/export`。

编码一致性在地区对比旁分层给出：`POST /reports/agreement` 按来源、议题和来源×议题列出每层已编码、双人一致、待裁定与裁定后的观察数与故事分母，采集覆盖不足与已观察一致性分列，互不填补；`POST /reports/agreement-contrast` 在地区对比旁给出逐来源剔除与争议处理两种情景下地区结论的变化；`POST /reports/decision-impact` 追溯单条意见对分层、故事与已发布版本的影响；`GET /releases/{release_id}/change-causes` 把发布后的变化分账为来源结构与编码争议两类，不合成单一分数。裁定后的案件永远单独成层，不回算成双人一致；更正后的旧版意见只解释旧版稿件。

运行 `python -m unittest discover -s tests -v`、`python -m compileall -q meridian` 和 `python -m meridian.demo`。数据库使用 SQLite；测试和演示使用临时本地数据库。
