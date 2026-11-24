# GFNA 风险清单、边界场景与兜底方案

> 评估日期：2026-11-24
> 评估范围：`backend/**`、`frontend/**`、`docker-compose.yml`、CI、Alembic、运行脚本
> 评估方法：静态代码走查 + 现有测试/CI 配置核对 + 隔离测试库实测
> 说明：本文只记录**当前代码中可证实或可复现**的风险；未验证的推测不列入。

---

## 0. 结论摘要

| 等级 | 数量 | 含义 |
|---|---:|---|
| P0 | 3 | 可能造成数据不可恢复、链路长期停摆或关键回归漏检 |
| P1 | 10 | 会造成业务错误、监控失真或恢复困难，需要近期处理 |
| P2 | 8 | 边界/运维/体验问题，按计划处理即可 |

当前项目已经具备的防护：统一错误体、数据新鲜度、质量不变量、演示模式只读、
Markdown 安全渲染、LLM 降级、迁移链、localhost 绑定、推送幂等、盘后链路门禁。
本清单重点是可进一步收紧的部分。

### 实施状态（2026-11-24）

| 风险 | 状态 | 落地内容 |
|---|---|---|
| R-01 备份 | 已实施 | 备份服务、迁移前备份、恢复演练脚本、compose 备份卷 |
| R-02 数据重采 | 已实施 | 巡检按 `DAILY_CHAIN_MAX_DATA_RETRIES` 触发一次补采 |
| R-03 CI API 回归 | 已实施 | CI 集成任务灌全历史数据并运行 `test_api_v5.py` |

R-01 的“每月恢复演练”仍需人工执行或后续加入定时任务；本次已提供可重复运行的脚本。
实测：生产库备份 1.24 GB 成功；恢复演练（快速模式）表数 56、`fact_stock_daily`
5,293,867 行、`dim_stock` 5,957、`stg_run` 2,940、最新交易日 2026-11-23，源库与恢复库一致。

---

## 1. P0 风险

### R-01 数据库没有自动备份与恢复演练

- **产生原因**：`docker-compose.yml` 只声明了命名卷 `postgres_data`，仓库内没有
  `pg_dump` 定时任务、备份保留策略或恢复脚本；`backend/artifacts/` 中的
  `restore_real.*` 只是历史一次性操作产物。
- **影响范围**：全库行情、特征、评分、回测、纸面组合、新闻与任务记录。误执行
  `docker compose down -v`、卷损坏、磁盘故障或迁移失败都可能造成不可恢复丢失。
- **严重程度**：P0。
- **解决方案**：
  1. 每日收盘链路完成后执行 `pg_dump -Fc`，保留最近 7 份日备份 + 4 份周备份；
  2. 备份文件写到宿主机独立目录（不要放在 Docker 卷内），并记录校验和；
  3. 每月在临时库执行一次恢复演练，比对关键表行数与最新交易日。
- **兜底处理**：
  - 迁移前先自动触发一次备份；备份失败则阻止迁移；
  - 恢复时使用 `pg_restore --clean --if-exists` 到新库，验证后再切换；
  - 至少保留一份异地/移动硬盘副本。
- **验证方法**：
  ```bash
  docker compose exec -T postgres pg_dump -U postgres -Fc gfna_db > gfna_$(date +%F).dump
  createdb -h 127.0.0.1 -p 5433 gfna_restore_test
  pg_restore -h 127.0.0.1 -p 5433 -d gfna_restore_test gfna_$(date +%F).dump
  psql -h 127.0.0.1 -p 5433 -d gfna_restore_test -c "select count(*), max(trade_date) from fact_stock_daily;"
  ```
  与生产库对比行数和最新交易日一致；恢复后的库可启动 API 并返回 `/health`。

### R-02 数据阶段失败后，盘后巡检不会自动重采

- **产生原因**：[daily_chain.py](../backend/app/daily_chain.py) 的巡检与启动补跑
  使用 `run_data=False`，只续跑罗盘/推送；数据门禁失败时返回 `blocked`，不会重跑
  `run_daily_pipeline`。这是为了避免每 30 分钟重复触发全量采集。
- **影响范围**：15:36 采集因网络/数据源失败后，若进程一直在线且不重启，当天数据
  可能一直缺失，罗盘与推送也不会执行；只能等下一次重启补跑或人工触发。
- **严重程度**：P0（链路长期停摆）。
- **解决方案**：
  1. 巡检在“数据未就绪 + 数据流水线锁空闲 + 当日尚未重试过”时，触发一次
     `run_data=True`（建议每天最多 1 次，且限定在 17:00 前）；
  2. 或增加独立的重采任务：17:05 检查当日数据，缺失则跑一次完整采集；
  3. 将“当日重采次数/最后尝试时间”写入 `ingest_job.checkpoint`，避免风暴。
- **兜底处理**：
  - 重采仍失败时保留 `blocked` 状态并发送一次告警；
  - 提供 `POST /api/v1/admin/daily-chain?run_data=true` 人工恢复；
  - 次日启动补跑按历史口径补齐。
- **验证方法**：
  - 在隔离测试库断网/注入采集异常，确认首次失败告警、二次巡检触发一次重采、
    重采成功后罗盘与推送继续；
  - 查询 `ingest_job where task='daily_chain'` 的 checkpoint，确认重试次数受限。

### R-03 API 回归测试未在 CI 中执行

- **产生原因**：[ci.yml](../.github/workflows/ci.yml) 的 `backend` 任务以
  `pytest tests -q` 运行，但未设置 `GFNA_TEST_DB`，而
  [test_api_v5.py](../backend/tests/test_api_v5.py) 顶部有
  `pytestmark = skipif(not TEST_DB_ENABLED)`，因此整套 API 用例被跳过；
  `backend-integration` 任务只运行 `tests/integration`，不覆盖 `test_api_v5.py`。
- **影响范围**：API 契约、分页、错误体、回测/选股接口的回归不会被 CI 发现，
  只能靠本地手工执行。
- **严重程度**：P0（关键回归漏检）。
- **解决方案**：
  1. 在 `backend-integration` 任务中增加“灌演示数据 + 运行 `test_api_v5.py`”步骤；
  2. 或新增独立 `api-tests` job：`alembic upgrade head` → `pipeline_cli demo` →
     跑 `features/scores/sector_scores` → `pytest tests/test_api_v5.py`；
  3. CI 中保留 `GFNA_TEST_DB=1`，并断言 skip 数量不超过预期。
- **兜底处理**：
  - 本地发布前固定执行 `pytest tests/test_api_v5.py`；
  - 在 README/发布清单中把该命令列为必跑项。
- **验证方法**：
  - 观察 CI 日志中 `test_api_v5.py` 的用例数为 0 还是实际数量；
  - 人为改坏一个 API 响应字段，确认 CI 失败。

---

## 2. P1 风险

### R-04 外部数据源单点与接口漂移

- **产生原因**：行情、板块、资金流、两融、ETF、新闻均依赖第三方公开接口
  （AKShare、东财、同花顺、新浪、腾讯等）。接口字段、反爬策略或可用性变化不受控。
- **影响范围**：采集失败或字段缺失会沿“行情 → 特征 → 评分 → 罗盘 → 推送”传导。
- **严重程度**：P1。
- **解决方案**：
  - 保持 Provider 链与兜底源，新增数据源时实现同一接口并纳入
    `ProviderChain`；
  - 对关键数据集设置“连续 N 次失败”告警，而不是只看单次；
  - 记录 `meta_provider_health` 并在 `/jobs` 页面展示降级趋势。
- **兜底处理**：
  - 单源失败自动切兜底源；
  - 全部失败时保留上一交易日数据并明确标记 `stale`，禁止用旧数据冒充当日；
  - 盘后链路数据门禁会阻止罗盘/推送继续。
- **验证方法**：
  - 用假 Provider 注入异常，确认降级链与 `provider_health` 记录；
  - 断网运行采集，确认接口返回 503/明确错误，而不是 500 或空数组。

### R-05 部分成功被标记为成功

- **产生原因**：[pipeline.py](../backend/app/pipeline.py) 的 `run_task` 对多数任务
  以质量状态决定成败；部分采集函数返回 `failed>0` 但质量状态仍可能为 `ok`。
  现有 `_BULK_BACKFILL_TASKS` 只覆盖回补类任务。
- **影响范围**：少量标的缺失时，评分/罗盘可能基于不完整样本；任务监控显示绿色。
- **严重程度**：P1。
- **解决方案**：
  - 明确“可接受失败比例”并在 `run_task` 中统一判断：超过阈值即
    `partial/quality_failed`；
  - 对关键数据集（`stock_daily`、`features`、`scores`、`sector_scores`）设置
    覆盖率下限，低于门槛不写 `ok`；
  - 把 `failed>0 but status=success` 纳入不变量检查。
- **兜底处理**：
  - 质量不达标时状态置 `partial`，链路门禁拒绝继续；
  - 保留上一份成功数据并标记 `stale`。
- **验证方法**：
  - 构造 1% 标的缺失的采集结果，确认状态与前端徽标为 `partial`；
  - 查询 `meta_data_quality` 确认 `coverage` 与 `status` 一致。

### R-06 数据不变量违规只记录日志，不阻断链路

- **产生原因**：`log_invariants` 在流水线结束时只输出 warning/info，
  盘后链路的 `data_readiness` 只检查数据集状态与 `stg_run`，不会读取不变量报告。
- **影响范围**：SCD2 重叠、僵尸任务、覆盖率异常等结构性问题可能不阻止罗盘/推送。
- **严重程度**：P1。
- **解决方案**：
  - 将 `collect_invariants` 的 error 级检查纳入 `data_readiness`；
  - 或至少把 error 级违规转为链路失败并告警；
  - 保留 warning 级不阻断，避免过度敏感。
- **兜底处理**：
  - 不变量检查自身异常时只记录日志，不影响主链路；
  - 违规结果写入 `ingest_job.checkpoint.detail` 供排查。
- **验证方法**：
  - 人为制造 `dim_sector_member` 时间重叠，确认链路 blocked 且告警；
  - 恢复数据后重跑，确认链路继续。

### R-07 进程离线恢复后的“迟到推送”

- **产生原因**：`catch_up` 会补齐最近缺失交易日，`daily_chain` 随后按最近完整
  交易日补生成罗盘并推送。若离线跨日，恢复后可能推送前一交易日的信号。
- **影响范围**：用户可能在次日盘中收到“昨日选股”消息，产生误用风险。
- **严重程度**：P1。
- **解决方案**：
  - 为推送增加“有效期”判断：仅当目标交易日是最近一个交易日且当前时间在
    T+1 开盘前才自动推送；
  - 超期时只生成罗盘/落库，不自动推送，改为站内提示或“补发”手动入口；
  - 消息标题明确标注“补发/迟到”。
- **兜底处理**：
  - 保留 `push_cli.py --date` 手动补发能力；
  - 所有推送记录写 `push_log`，可审计。
- **验证方法**：
  - 模拟进程在 T+1 10:00 启动，确认默认不自动推送旧日信号；
  - 手动补发时消息日期与内容正确。

### R-08 Alembic 与 `AUTO_CREATE_TABLES` 双轨漂移

- **产生原因**：生产模板默认 `AUTO_CREATE_TABLES=1`，启动时 `create_all` 只补缺失
  表，不做列变更；已有库的列变更必须靠 Alembic。若漏跑迁移，ORM 查询会报缺列。
- **影响范围**：升级后接口 500、写入失败；测试库与生产库结构不一致。
- **严重程度**：P1。
- **解决方案**：
  - 生产环境建议 `AUTO_CREATE_TABLES=0`，启动前强制 `alembic upgrade head`；
  - 启动日志打印当前 Alembic 版本并与代码期望版本比对；
  - CI 增加 `upgrade head → downgrade -1 → upgrade head` 三步验证。
- **兜底处理**：
  - 启动时检测关键列是否存在，缺失则拒绝启动并提示迁移；
  - 迁移前自动备份（见 R-01）。
- **验证方法**：
  - 在旧版本测试库执行 `alembic upgrade head`，确认新列存在；
  - 人为跳过一个迁移，确认启动自检失败而不是运行中 500。

### R-09 长事务、连接池与 advisory lock 悬挂

- **产生原因**：`session.py` 设置了 `idle_in_transaction_session_timeout=900000`
  （15 分钟）；长时间纯计算且不发 SQL 的事务可能被 PG 终止。连接池
  `pool_size=10 + max_overflow=10`，并发回测/采集/前端轮询可能耗尽。
- **影响范围**：任务中途失败、连接超时、锁随会话释放导致并发任务重入。
- **严重程度**：P1。
- **解决方案**：
  - 长计算任务分段提交或定期 `SELECT 1` 保活；
  - 回测/采集使用独立连接池或限制并发；
  - 锁操作使用独立短会话，避免长事务持有 advisory lock；
  - 监控 `pg_stat_activity` 中 `idle in transaction` 与会话数。
- **兜底处理**：
  - PG 自动回收僵尸会话；
  - 任务恢复机制把遗留 `running` 标为 `failed`；
  - 重跑幂等。
- **验证方法**：
  ```sql
  select pid, state, wait_event_type, now()-state_change as idle_for, query
  from pg_stat_activity where datname='gfna_db' order by idle_for desc;
  ```
  人为 kill 一个持锁会话，确认下一次任务能重新获取锁。

### R-10 告警单通道、Webhook 未配置与推送“至少一次”

- **产生原因**：链路告警只走企业微信 `WECOM_WEBHOOK_URL`；未配置时只记日志。
  `notify/push.py` 与 `compass/push.py` 都是“先发送、后提交状态”，若发送成功但
  数据库提交失败，重试会重复发送。
- **影响范围**：关键失败无人知晓；或用户收到重复消息。
- **严重程度**：P1。
- **解决方案**：
  - 至少增加一个本地兜底：失败时写 `chain_alert` 表/文件并在 `/jobs` 页高亮；
  - 告警去重已有 `push_log(run_id, channel)`，可扩展到邮件/桌面通知；
  - 推送日志先写“pending + 幂等键”，发送成功后再置 `sent`（现状已接近）。
- **兜底处理**：
  - Webhook 未配置时在启动日志中明确警告；
  - 重复推送可接受（至少一次），消息含日期，避免误读。
- **验证方法**：
  - 清空 `WECOM_WEBHOOK_URL`，确认日志告警且不影响链路；
  - 模拟发送成功后提交失败，确认重试行为与重复消息是否符合预期。

### R-11 管理接口鉴权的代理边界

- **产生原因**：`require_admin` 在未配置 `ADMIN_API_KEY` 时只信任
  `127.0.0.1/::1/localhost/testclient`。Docker 中浏览器经 Nginx 访问后端时，
  后端看到的客户端是 Nginx 容器 IP，不是 `127.0.0.1`，因此管理操作会 403。
- **影响范围**：未设置密钥时，任务监控页的补数/重跑按钮不可用；设置密钥后正常。
- **严重程度**：P1（可用性问题，不是漏洞）。
- **解决方案**：
  - 在 README/启动脚本中明确：使用管理按钮必须配置 `ADMIN_API_KEY`；
  - 或在 Nginx 层注入并校验管理密钥（不推荐把密钥放前端）；
  - 不建议放宽 IP 白名单来“修复”。
- **兜底处理**：
  - 本机命令行直接访问 `127.0.0.1:8000` 并带 `X-Admin-Key`；
  - 前端在 403 时提示“请配置管理员密钥”。
- **验证方法**：
  - 空密钥时经 `http://127.0.0.1:5173` 调管理接口应 403；
  - 配置密钥后同路径应 200；直接 8000 端口带密钥应 200。

### R-12 单机单进程与单数据库 SPOF

- **产生原因**：架构定位是单机自用：一个 FastAPI 进程、一个 PostgreSQL、
  APScheduler 内存作业。没有主从、队列或进程守护（仅有可选 watchdog 脚本）。
- **影响范围**：进程崩溃/主机休眠期间任务不执行；数据库故障则全站不可用。
- **严重程度**：P1。
- **解决方案**：
  - 短期：Docker `restart: unless-stopped` + 开机自启 + 外部定时唤醒；
  - 中期：把调度器拆为独立常驻进程，API 可独立重启；
  - 数据层：定期备份 + 可快速恢复（R-01）。
- **兜底处理**：
  - 启动补跑 + 盘后巡检恢复错过任务；
  - 任务状态落库，重启后可审计。
- **验证方法**：
  - 杀进程后重启，确认 `catch_up` 与链路恢复；
  - 停止数据库 5 分钟，确认 API 返回 `DB_UNAVAILABLE` 而不是挂死。

### R-13 前端缺少全局错误边界与 chunk 加载兜底

- **产生原因**：`App.tsx` 只有 `Suspense` 懒加载边界，没有 React ErrorBoundary；
  动态 import 失败（发布后旧 chunk 被删、弱网）会让页面空白。
- **影响范围**：单个组件异常导致整页不可用；用户无法自助恢复。
- **严重程度**：P1。
- **解决方案**：
  - 增加全局 `ErrorBoundary`，展示错误码、刷新与返回首页按钮；
  - 对 `lazy()` 的 import 失败做一次自动刷新/重试；
  - 保留 `Suspense` 骨架，避免白屏。
- **兜底处理**：
  - 捕获后清理过期会话状态；
  - 提供“重新加载”入口。
- **验证方法**：
  - 人为让某个 chunk 404，确认展示错误页而非白屏；
  - 组件内抛错，确认边界捕获且导航仍可用。

---

## 3. P2 风险

### R-14 默认口令、CORS 与无 TLS

- **原因**：compose 在缺少 `.env` 时使用默认口令 `gfna_secret`；
  `CORS_ORIGINS` 默认 `*` 且 `allow_credentials=True`；本机 HTTP 无 TLS。
- **影响**：若未来把端口改绑 `0.0.0.0` 或做端口转发，默认口令与宽松 CORS 会放大风险。
- **级别**：P2（当前仅绑定 `127.0.0.1`，风险受限）。
- **方案/兜底**：强制首次启动生成随机口令；生产显式设置 `CORS_ORIGINS`；
  远程访问走 SSH 隧道或反向代理 + TLS；启动日志检测默认口令并警告。
- **验证**：`docker compose config` 检查口令来源；浏览器跨源请求确认只允许配置来源。

### R-15 日志无轮转，磁盘可能被写满

- **原因**：日志只输出 stdout，compose 未配置 `logging.options.max-size`；
  Docker 默认 `json-file` 可无限增长。
- **影响**：长期运行后磁盘占满，数据库写入失败。
- **级别**：P2。
- **方案/兜底**：compose 增加 `max-size: 50m`、`max-file: 5`；或接入外部日志。
- **验证**：`docker inspect gfna-backend --format '{{.HostConfig.LogConfig}}'`；
  连续压测后检查日志文件大小。

### R-16 依赖版本老化与无漏洞审计

- **原因**：`requirements.txt` 固定了较早版本（FastAPI 0.110、httpx 0.27.2、
  Playwright 1.42 等）；CI 只安装与测试，不执行漏洞扫描。
- **影响**：已知 CVE、上游兼容性问题、升级成本累积。
- **级别**：P2。
- **方案/兜底**：每月执行 `pip-audit` / `npm audit`；建立升级窗口；
  升级前跑全量测试与浏览器验收。
- **验证**：`pip-audit -r backend/requirements.txt`、`npm audit --omit=dev` 输出为 0 高危。

### R-17 时区与交易日历边界

- **原因**：调度使用 `date.today()` 与 Asia/Shanghai；交易日历缺失时会联网补齐。
  容器已设置 TZ，但宿主机/裸机运行若时区不同可能偏移。
- **影响**：跨日边界可能把任务算到错误交易日；离线且日历缺失时判断失败。
- **级别**：P2。
- **方案/兜底**：统一用 `ZoneInfo("Asia/Shanghai")` 取日期；启动时检查日历覆盖
  未来 30 天；缺失时用最近已知日历并明确告警。
- **验证**：把系统时间调到 23:59/00:01，确认目标交易日与消息日期一致；
  清空未来日历后确认启动告警。

### R-18 测试数据依赖与跳过用例

- **原因**：`test_api_v5.py` 需要 `_test` 库与演示数据，未满足时整组跳过；
  前端 CI 只跑 treemap/sparkline/palette，`sample/compass` 测试未纳入。
- **影响**：本地“全绿”可能掩盖未执行用例；覆盖率数据不可信。
- **级别**：P2（与 R-03 的 CI 缺口互补）。
- **方案/兜底**：为测试提供固定 fixture/种子脚本；CI 汇总 skipped 数量并设置上限；
  前端五个测试脚本全部纳入 CI。
- **验证**：`pytest -q -rs` 查看 skip 原因；CI 日志中断言 skipped 数。

### R-19 文档与实现漂移

- **原因**：项目文档多（README、wiki、历史任务书、契约），部分历史文档描述的是
  当时行为；`check_doc_routes` 只校验路由表，不校验时间表、行号与配置。
- **影响**：按旧文档操作可能得到错误结果。
- **级别**：P2。
- **方案/兜底**：区分“现行文档”和“历史任务书”；现行文档变更时同步更新；
  路由/配置用脚本校验，时间表纳入发布检查。
- **验证**：`python scripts/check_doc_routes.py`；随机抽查现行文档中的时间/命令。

### R-20 演示模式与演示数据误用

- **原因**：`ENV=demo` 才允许 seed；`VITE_DEMO_MODE=1` 时前端只读本地快照。
  但演示快照有生成日期，可能被误认为实时数据。
- **影响**：用演示数据做真实决策；或演示构建被部署到生产。
- **级别**：P2。
- **方案/兜底**：演示构建显示醒目横幅与生成日期；生产构建禁用 demo 环境变量；
  启动时校验 `ENV` 与数据源开关。
- **验证**：`VITE_DEMO_MODE=1 npm run build` 后检查横幅与网络请求均为本地 JSON；
  生产启动日志确认 `ENV != demo`。

### R-21 变更管理与未提交工作区

- **原因**：当前工作区同时存在自选股清理、V10 任务监控、盘后链路三组未提交改动。
- **影响**：误提交、覆盖、回滚困难；难以区分回归来源。
- **级别**：P2（流程风险）。
- **方案/兜底**：按主题拆分提交；提交前跑全量测试；用分支/tag 标记可回滚点。
- **验证**：`git status`、`git diff --stat` 按主题分组；每个提交单独可回滚。

---

## 4. 已有防护（不需要重复建设）

| 防护 | 位置 | 说明 |
|---|---|---|
| 统一错误体 | `app/core/errors.py`、`app/main.py` | 业务异常与未处理异常都有结构化响应 |
| 数据新鲜度 | `meta_data_quality` + `/meta/freshness` | 明确“上次成功/本次失败” |
| 任务恢复 | `services/task_recovery.py` | 重启清理遗留 running |
| 数据不变量 | `quality/invariants.py` | SCD2/覆盖率/僵尸任务检查 |
| 链路口禁 | `app/daily_chain.py` | 数据/罗盘/推送串行且可恢复 |
| 推送幂等 | `push_log`、`compass_push_log` | 同一 run/card 不重复 |
| 前端安全渲染 | `Markdown.tsx`、`palette.ts` | 不使用 `dangerouslySetInnerHTML` |
| LLM 降级 | `services/llm_service.py` | 失败降级模板/空摘要，不阻断 |
| 演示只读 | `api/demo.ts` | 写操作直接拒绝 |
| 端口绑定 | `docker-compose.yml` | 仅 `127.0.0.1` |

---

## 5. 建议实施顺序

| 优先级 | 事项 | 预计改动 |
|---|---|---|
| 立即 | R-01 备份脚本 + 恢复演练；R-02 数据失败一次重采；R-03 CI 跑 API 用例 | 小 |
| 近期 | R-05 部分成功率门槛；R-06 不变量纳入门禁；R-07 推送有效期；R-08 迁移自检 | 中 |
| 近期 | R-09 连接池/长事务监控；R-10 告警兜底；R-13 ErrorBoundary | 中 |
| 计划 | R-14~R-21 运维、依赖、文档、变更管理 | 小~中 |

---

## 6. 统一验证清单

```bash
# 后端单元（不需要数据库）
cd backend
..\.venv\Scripts\python.exe -m pytest tests -q --ignore=tests/integration

# 隔离测试库：迁移 + 集成
$env:GFNA_TEST_DB="1"
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m pytest tests/integration -q

# API 契约与路由
..\.venv\Scripts\python.exe scripts\check_doc_routes.py

# 前端构建与纯函数测试
cd frontend
npm run build
npm run test:treemap; npm run test:sparkline; npm run test:palette
npm run test:sample; npm run test:compass

# 发布前人工检查
docker compose config                     # 端口/口令/CORS/日志配置
docker inspect gfna-backend --format '{{.HostConfig.LogConfig}}'
psql -h 127.0.0.1 -p 5433 -U postgres -d gfna_db -c "select * from meta_data_quality order by last_attempt_at desc limit 10;"
```

---

## 7. 残余风险说明

- 第三方数据源行为无法完全预测，只能通过兜底源、质量门槛与告警降低影响；
- 单机架构决定了主机故障期间系统不可用，恢复能力取决于备份与补跑；
- 本文基于当前代码与已执行测试，未做长时间稳定性压测与安全渗透测试；
  建议在正式对外前补充这两类验证。
