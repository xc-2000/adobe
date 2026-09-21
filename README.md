# firefly-gen · 生图 / 生视频 多用户中转平台

企业级 SaaS 后台风格（左右布局、蓝色科技感）的 AIGC 中转平台。用户在统一面板登录、在创作中心提交生图 / 图片编辑 / 生视频请求，平台通过一套自建的 **ARP 池 + 指纹自洽提交** 链路调度上游 Adobe Firefly（gpt-image / veo / luma / sora / seedance / firefly-image-5 / firefly-video 等模型），并做多用户计费、Key 管理与高并发调度。对外提供 OpenAI 与 Gemini 官方格式的同步/异步接口。

> 面向开发者的完整设计规则与文档见 [`docs/`](./docs)，AI 协作规则见 [`.cursor/rules/`](./.cursor/rules)。

## 技术栈

| 层 | 选型 |
|---|---|
| 后端 | Python 3.11 · FastAPI · SQLAlchemy 2.0 (async) · PostgreSQL · Redis · Celery |
| 出图链路 | curl_cffi（TLS 指纹模拟提交）+ 自建 ARP 池 |
| 采集器 | CloakBrowser PRO / fingerprint-chromium（148 指纹，住宅出口）+ Playwright |
| 前端 | React 18 · TypeScript · Ant Design (Pro Layout) · Vite |

## 系统全景（一句话）

出图成功 = 一个**住宅出口、指纹自洽、持续重铸**的 ARP 工厂，喂给一池**付费(HARD)账号**，提交时**绑铸币出口 + 对齐铸币身份**，并在**时间成簇的 408 上跨池重试骑好窗口**。缺任何一条就是 408/403。详见 [`docs/02-arp-core.md`](./docs/02-arp-core.md)。

```
[采集器 CloakBrowser PRO / fp-chromium 148 · 住宅出口]
   └─ SherlockSdk.initAsync() 铸 {sid,ark,bfp,ftr} + 抓 UA/Client-Hints
       └─ POST /admin-api/adobe/arp  →  ARP 池 (Redis, 带 egress + 身份 + TTL)
                                              │ checkout
[生成 worker · 纯 curl_cffi]  ← 账号 token(clio) + 整套 ARP bundle
   ├─ 从铸它的同一 sticky 出口提交
   ├─ 用铸它那台浏览器的 UA/CH 提交
   ├─ 408 → invalidate + 换票/换号重试
   └─ 200 → 轮询 href → 直连 CDN 下载
```

## 目录结构

```
firefly-gen/
├── .cursor/rules/     # AI 开发规则（MDC）
├── docs/              # 设计文档（架构 / ARP 核心 / 数据模型 / API / 前端 / 并发优化）
├── backend/           # FastAPI 后端（面板 + admin-api + ARP 池 + 生成调度）
├── frontend/          # React + Ant Design 后台
└── harvester/         # 独立 ARP 采集器进程
```

## 快速开始（开发）

参见 [`docs/07-dev-setup.md`](./docs/07-dev-setup.md)。最小路径：

```bash
docker compose up -d --build   # API :18000 + PostgreSQL + Redis + MinIO

# 或直接运行后端
cp .env.example .env      # 填 DATABASE_URL / REDIS_URL / ARP_INTERNAL_TOKEN
# 后端
cd backend && pip install -e ".[dev]" && uvicorn app.main:app --reload
# 前端
cd frontend && pnpm install && pnpm dev
# 采集器（独立机器/住宅出口）
cd harvester && python harvester.py
```

## 合规声明

本项目为技术研究与内部工程实现。上游账号、代理凭据、Cookie 等敏感数据一律加密存储、绝不入库明文、绝不提交仓库。见 [`.cursor/rules/50-security.mdc`](./.cursor/rules/50-security.mdc)。
