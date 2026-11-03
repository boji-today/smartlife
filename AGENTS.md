# Agent 约定

用 `.venv/bin/python`。tinytuya 固定 1.20.0，与镜像一致，不要装到系统 Python。

## 密钥

不要打印、提交或写进文档：API Secret、local key，以及 `.env`、`init.sql`、`tinytuya.json`、`devices.json`、`tuya-raw.json`、`snapshot.json`、`data/smartlife.db`。

## 操作

- 局域网扫描必须在沙箱外做。监听 6666 / 6667 / 7000 会收到其他网卡上的广播。
- 云端 `PROJECT`（data center suspended）时不要改密钥去猜区域。
- 查 `data/smartlife.db` 用 `file:<绝对路径>?immutable=1`。普通打开会因 WAL 在沙箱里失败（错误 14）。
- 本机 `collect.py` 与容器不要同时跑，否则同一时段会各插入一行。
- `readings.ts` 是 `device.status()` 返回后的 UTC 秒，不是 cron 整点。
