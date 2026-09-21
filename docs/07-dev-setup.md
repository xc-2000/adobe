# 07 · 本地开发与部署

## 依赖
- Python 3.11+、Node 18+（pnpm）、PostgreSQL 15+、Redis 7+。
- 采集器另需 CloakBrowser PRO / fingerprint-chromium（148）与住宅 sticky 代理。

## 环境变量
```bash
cp .env.example .env
# 必填：APP_SECRET_KEY, FIELD_ENCRYPTION_KEY, DATABASE_URL, REDIS_URL, ARP_INTERNAL_TOKEN
```

本机真实链路测试可设置：
```text
TEST_COOKIE_FILE=C:\Users\Administrator\Desktop\cookies_export (2) (1).txt
```
该文件仅运行时读取，不复制进仓库/镜像，不输出内容；测试账号入库前必须加密。

## 后端
```bash
cd backend
python -m venv .venv && . .venv/Scripts/activate   # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
alembic upgrade head          # 建表/迁移
uvicorn app.main:app --reload --port 8000
# worker（另开终端）
celery -A app.workers.celery_app worker -l info -c 32
```
建议依赖：`fastapi uvicorn[standard] sqlalchemy[asyncio] asyncpg alembic pydantic-settings redis celery curl_cffi httpx passlib[argon2] python-jose cryptography orjson`。

## 前端
```bash
cd frontend
pnpm install
pnpm dev        # http://127.0.0.1:5173，代理 /api → :8000
pnpm build      # 产物 dist/
```
建议依赖：`react react-dom antd @ant-design/pro-components @ant-design/charts @tanstack/react-query axios react-router-dom zustand`。

## 采集器（独立进程 / 独立机器）
```bash
cd harvester
pip install -r requirements.txt
python -m playwright install chromium   # 或指向 CloakBrowser/fingerprint-chromium
# 关键 env：HARVESTER_TARGET, ARP_INTERNAL_TOKEN, ARP_PROXIES(住宅sticky), ADOBE_CHROME_BINARY, ARP_INTERVAL=75
python harvester.py
```
验证：采集器日志每周期 `published N ARP`；`GET /admin-api/adobe/arp` 返回 `present:true, ready>0, oldest_age_sec` 小。

## 冒烟校验（对齐 [02 §10](./02-arp-core.md)）
1. 铸出可解码 `sid+ark+bfp+ftr`。
2. checkout 后提交代理与铸票一致（抓出口 IP）。
3. 付费 HARD 号打一次 → 200 出图；故意换 SID → 408。
4. 内容拒绝(451) 如实返回、不罚号。

## 部署（生产）
- 面板 + DB + Redis 在主机（HTTPS 反代，`admin-api` 仅内网 + IP 白名单）。
- worker 多实例，出口绑住宅代理；采集器部署在住宅出口机器，按出口分布多实例合并入同一池。
- 进程守护用 systemd / pm2 / supervisor；日志集中收集并脱敏。
- 容器化拓扑、harvester 容器限制、Secret/网络/回滚见 [`10-container-deployment.md`](./10-container-deployment.md)。
