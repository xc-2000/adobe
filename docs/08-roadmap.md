# 08 · 里程碑 / 开发阶段

按"先打通链路、再做面板、后做优化"推进。每阶段有可验收产物。

## 阶段 0 · 骨架（本阶段产物：规则 + 文档 + 目录）
- [x] 开发规则 `.cursor/rules/`、设计文档 `docs/`、`.env.example`、目录骨架。

## 阶段 1 · 出图链路打通（最高优先）
- [ ] `harvester`：148 指纹 + 住宅 sticky 铸 `sid/ark/bfp/ftr`，抓 UA/CH，POST 入池。
- [ ] `arp` 池（Redis）：入池 / checkout(最新鲜) / invalidate / health。
- [ ] `adobe`：curl_cffi 提交(绑出口+身份) + 轮询 + CDN 直连下载；cookie→clio token 交换。
- [ ] `payloads`：gpt-image / veo / luma / sora / seedance / image5 / firefly-video 请求体。
- [ ] 验收：付费 HARD 号 → 200 出图；换 SID → 408；内容拒绝 451 不罚号。

## 阶段 2 · 后端平台
- [ ] 数据模型 + Alembic 迁移（users/keys/jobs/records/ledger/accounts/logs/config）。
- [ ] 面板鉴权(JWT) + 用户/管理员 API；生成入队(Celery) + 幂等 + 计费(原子扣减+ledger)。
- [ ] 对外 `/v1` 兼容接口 + KEY 鉴权 + 限流；request_log 脱敏审计。

## 阶段 3 · 前端 SaaS 后台
- [ ] ProLayout 左右布局 + 蓝色主题；角色分区路由与菜单。
- [ ] 用户：创作中心(WS 进度)/生成历史/KEY/调用说明/余额明细/个人设置。
- [ ] 管理员：数据统计(图表+池水位)/用户管理/充值·消费记录/系统配置/请求日志。

## 阶段 4 · 高并发与优化
- [ ] 账号池租约调度 + park/换号骑好窗口 + egress 评分。
- [ ] 分层限流、缓存、连接池调优、日志分区。
- [ ] 可观测（指标 + 告警）+ 自愈（采集器/worker/账号 token）。
- [ ] 横向扩展（面板/worker/采集器多实例，DB 读写分离/分区）。

## 阶段 5 · 加固
- [ ] 敏感数据加密(KMS/Fernet)、密钥轮换、越权(IDOR)审计。
- [ ] 压测、混沌演练（池空/账号大面积 dead 的降级）。
