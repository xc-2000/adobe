# harvester · ARP 采集器（独立进程）

CloakBrowser PRO / fingerprint-chromium（148 指纹）+ 住宅 sticky 出口，持续铸 `sid/ark/bfp/ftr` 并抓 UA/Client-Hints，POST 到平台 `admin-api/adobe/arp` 入池。**与业务库零耦合，只走 HTTP。**

原理与不变量见 [`../docs/02-arp-core.md`](../docs/02-arp-core.md) 与 [`../.cursor/rules/30-arp-core.mdc`](../.cursor/rules/30-arp-core.mdc)。运行见 [`../docs/07-dev-setup.md`](../docs/07-dev-setup.md)。

## 配置

- `CLOAKBROWSER_LICENSE_KEY`：可选；未设置时 CloakBrowser 自动读取 `~/.cloakbrowser/license.key`。
- `CLOAKBROWSER_VERSION=148.0.7778.215.5`：固定 Chrome 148。
- `ARP_PROXY_FILE=../secrets/arp_proxies.txt`：一行一条代理；文件位于 gitignored `secrets/`。
- `ARP_PROXY_SCHEME=http`：没有 scheme 的代理使用此默认值。
- `HARVESTER_TARGET=http://127.0.0.1:18000`、`ARP_INTERNAL_TOKEN`。
- `HARVESTER_ONCE=1`：只铸一枚，用于受控冒烟测试。
- `HARVESTER_BLOCK_RESOURCE_TYPES`：默认空以保护 ARP 质量；建立 A/B 基线后才可尝试 `media` 或 `image`。

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
$env:HARVESTER_ONCE = "1"
.\.venv\Scripts\python -m arp_harvester.main
```

日志只输出 ARP id、egress id 和掩码后的出口 IP，不输出代理账密、ARP token 或 CB Key。
