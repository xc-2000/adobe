# 00 · 项目总览

## 是什么
firefly-gen 是一个**生图 / 生视频多用户中转平台**：用户在统一 SaaS 面板登录，在创作中心提交文生图 / 图生图 / 文生视频 / 图生视频请求，平台调度上游 Adobe Firefly（多模型）完成生成并计费。管理员在后台做数据统计、用户管理、计费、系统配置与请求审计。

**技术支持 QQ 群：19302577**

## 谁用
- **终端用户**：网页面板操作，或用平台签发的 API KEY（`sk-...`）调用 OpenAI 兼容风格接口。
- **管理员**：后台运营（用户、计费、配置、日志、监控）。
- **系统内部**：采集器进程、生成 worker 进程。

## 核心难点
身份按模型分流：**默认 FF-iOS 出图**（香蕉 / Image 2 / 视频，无 ARP）；仅 GPT Image 2.5 走网页 clio + ARP。2.5 的 `generate-async` 对不合格请求回 **408 "system under load"**（软风控拒绝），必须凑齐六个条件（见 [02-arp-core](./02-arp-core.md)）。平台技术含量集中在：
1. **FF-iOS 凭据**：Cookie 静默授权得到加密 device grant，每次任务现换移动端 AT，住宅代理提交。
2. **ARP 采集器（仅 2.5）**：住宅出口 + 148 指纹浏览器持续铸 sid/ark/bfp/ftr。
3. **ARP 池**：整套 bundle 入池分发，408 即作废。
4. **指纹自洽提交（仅 2.5）**：curl_cffi 用铸票出口 + 铸票身份提交。
5. **多用户高并发调度**：账号/ARP 作稀缺资源做队列、锁、限流、计费。

## 菜单（产品范围）
- **管理员**：数据统计 · 用户管理 · 充值记录/消费记录 · 系统配置 · 请求日志。
- **用户**：创作中心 · 生成历史 · KEY 管理 · 调用说明 · 余额明细 · 个人设置。

## 文档索引
| 文档 | 内容 |
|---|---|
| [01-architecture](./01-architecture.md) | 系统架构、进程拆分、数据流 |
| [02-arp-core](./02-arp-core.md) | ARP 六条件、铸票、提交协议（技术核心） |
| [03-data-model](./03-data-model.md) | 数据库表设计 |
| [04-api-spec](./04-api-spec.md) | 面板 / OpenAI / Gemini / 内网 API（同步、异步、编辑） |
| [05-frontend-ui](./05-frontend-ui.md) | 企业级 SaaS 后台 UI 规范与页面 |
| [06-concurrency-optimization](./06-concurrency-optimization.md) | 高并发与优化 |
| [07-dev-setup](./07-dev-setup.md) | 本地开发与部署 |
| [08-roadmap](./08-roadmap.md) | 里程碑与阶段 |
| [09-implementation-plan](./09-implementation-plan.md) | harvester → ARP 池 → 提交的开发与验收方案 |
| [10-container-deployment](./10-container-deployment.md) | Compose/生产容器拓扑、隔离、伸缩与回滚 |
| [12-minting-and-config](./12-minting-and-config.md) | **铸票 + 系统设置运行时备忘（后续功能真源）** |
| [13-models](./13-models.md) | **模型目录与上游请求参数（8 个正式模型；尺寸/头/408）** |
