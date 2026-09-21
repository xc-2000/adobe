# harvester · ARP 采集器（独立进程）

CloakBrowser PRO / fingerprint-chromium（148 指纹）+ 住宅 sticky 出口，持续铸 `sid/ark/bfp/ftr` 并抓 UA/Client-Hints，POST 到平台 `admin-api/adobe/arp` 入池。**与业务库零耦合，只走 HTTP。**

原理与不变量见 [`../docs/02-arp-core.md`](../docs/02-arp-core.md) 与 [`../.cursor/rules/30-arp-core.mdc`](../.cursor/rules/30-arp-core.mdc)。运行见 [`../docs/07-dev-setup.md`](../docs/07-dev-setup.md)。
