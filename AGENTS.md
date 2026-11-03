# Agent 约定

本地 TinyTuya 项目。用 `.venv/bin/python`，不要装到系统 Python。依赖为 tinytuya 1.20.0。

## 密钥

- 凭据只放 `.env`（模板是 `.env.example`）。
- 不要提交、打印或写进文档：API Secret、local key，以及 `.env`、`tinytuya.json`、`devices.json`、`tuya-raw.json`、`snapshot.json`。

## 网络

- 网卡和网段见 `AGENTS.local.md`。
- 扫描前确认网卡地址，发现广播只从真实网卡发出。不要依赖 tinytuya 默认选路；TUN/代理会导致广播失败并扫到 0 台设备。
- Wi-Fi 扫描要绑定实际网卡，广播到该网段的广播地址。监听端口是 6666 / 6667 / 7000，会收到其他网卡上的广播。
- 局域网扫描不能在沙箱网络里做。
- 没有 `devices.json` 时不要指望 `-force`；tinytuya 会关掉 force-scan。

## Wizard

读取 `.env` 后调用 `wizard`，带上 `-nocolor -yes` 以及 `-key`、`-secret`、`-region`、`-device`。区域未开通时云端返回 `PROJECT`（data center suspended），不要改密钥去猜区域。

## 已知设备

二氧化碳检测仪，category `co2bj`。地址、product 和 id 见 `AGENTS.local.md`。

DPS：`2` co2 ppm，`1` co2_state，`18` 温度 °C，`19` 湿度，`15` 电量。读状态用 `devices.json` 里的 local key，不要把 key 回写到仓库。
