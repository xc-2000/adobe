# 03 · 数据模型

PostgreSQL。金额/积分一律 `BIGINT` 最小单位（分 / 积分），禁 float。时间 `TIMESTAMPTZ`。敏感字段加密存储。

## 实体总览
```
users ─1:N─ api_keys
users ─1:N─ generation_jobs ─1:1─ consume_records
users ─1:N─ recharge_records
users ─1:N─ balance_ledger        (余额流水，充值/消费/退款统一记账)
upstream_accounts (账号池)         request_logs (审计)
arp_pool (Redis 为主，PG 可选快照)  system_configs (KV 配置)
```

## users — 用户
| 字段 | 类型 | 说明 |
|---|---|---|
| id | BIGINT PK | |
| username / email | text unique | 登录标识 |
| password_hash | text | argon2/bcrypt |
| role | enum(`user`,`admin`) | 权限 |
| balance | BIGINT | 当前余额/积分（最小单位），与 ledger 对账 |
| status | enum(`active`,`disabled`) | |
| created_at / last_login_at | timestamptz | |

## api_keys — 用户 KEY
| id | key_prefix (text, `sk-xxxx` 前几位，用于展示) | key_hash (text, sha256+salt) | user_id FK | name | status(`active`/`revoked`) | rate_limit(每分钟) | last_used_at | created_at |
- 明文 KEY 仅创建时返回一次；校验用 hash 常量时间比较 + Redis 缓存 `key_hash→user_id`。

## generation_jobs — 生成任务
| 字段 | 说明 |
|---|---|
| id / idempotency_key(unique) | 幂等键防重复扣费 |
| user_id FK / api_key_id FK(nullable) | 面板或 API 来源 |
| kind | enum(`image`,`video`) |
| model | 如 `firefly-gpt-image-2` / `veo31-fast` / `sora2` / `luma` / `seedance-2.0` |
| prompt / negative_prompt | text |
| params | jsonb（aspect_ratio, resolution, duration, seeds, reference_blob_ids...） |
| status | enum(`queued`,`running`,`succeeded`,`failed`,`rejected`) |
| cost | BIGINT 本次扣费（成功才 >0） |
| result_urls | jsonb（对象存储路径 / 签名 URL） |
| error_code / error_message | 失败/拒绝原因（脱敏） |
| retry_count | 上游重试次数 |
| created_at / started_at / finished_at | |

## consume_records — 消费记录
| id | user_id FK | job_id FK | amount(BIGINT) | model | unit_price | created_at |
- 与余额扣减、balance_ledger 同事务写入。

## recharge_records — 充值记录
| id | user_id FK | amount(BIGINT) | method(enum:manual/card/...) | operator_id(管理员,nullable) | remark | status | created_at |

## balance_ledger — 余额流水（对账真源）
| id | user_id FK | delta(BIGINT,正负) | balance_after | type(enum:`recharge`,`consume`,`refund`,`adjust`) | ref_type/ref_id(关联充值或消费) | created_at |
- 任何余额变动都在此留痕；`users.balance` 为缓存视图，以 ledger 为准做对账。

## upstream_accounts — 上游账号池（敏感，加密）
| 字段 | 说明 |
|---|---|
| id | |
| email | 账号标识（展示脱敏） |
| cookie_enc / access_token_enc / clio_token_enc | **加密存储**，绝不明文 |
| plan_cap | enum(`HARD`,`FREE`)，只用 HARD 出图 |
| group | text，如 `alpha`（可铸 clio 的号） |
| credits_remaining / credits_total | 额度快照 |
| status | enum(`active`,`parked`,`ratelimited`,`dead`) |
| parked_until | timestamptz（408 后临时冷却） |
| last_used_at / updated_at | |
- checkout 用 Redis 租约锁；408 → `parked` + `parked_until`；401 → 刷 token；持续失败 → `dead`。

## request_logs — 请求日志（审计，脱敏）
| id | job_id FK(nullable) | account_hash | egress_id | endpoint | status_code | latency_ms | retry_count | arp_id | outcome(`ok`/`408`/`403`/...) | created_at |
- **绝不**记录 token/cookie/代理密码明文。用于成功率(按 egress 累积)、耗时、审计。

## system_configs — 系统配置（KV）
| key(text PK) | value(jsonb) | updated_by | updated_at |
- 例：模型开关与定价、默认 impersonate、池水位阈值、限流阈值、公告等。热点走 Redis 缓存 + 主动失效。

## arp_pool — ARP 池
主存 **Redis**（低延迟、TTL 天然过期）：`arp:{id}` 存 bundle（见 [02-arp-core §3](./02-arp-core.md)），`arp:ready`(zset by expires) 供 checkout。PG 可选留快照仅用于监控统计，**不存明文代理密码**（或加密）。
