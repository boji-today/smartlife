# smartlife

用 [tinytuya](https://github.com/jasonacox/tinytuya) 在局域网发现并读取 Tuya / Smart Life 设备。

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install tinytuya
cp .env.example .env
```

`.env` 填写 Tuya IoT 云端凭据：

| 变量 | 说明 |
|---|---|
| `TUYA_API_KEY` | Access ID |
| `TUYA_API_SECRET` | Access Secret |
| `TUYA_API_REGION` | `cn` `us` `us-e` `eu` `eu-w` `in` `sg` |
| `TUYA_DEVICE_ID` | App 里已注册的任意设备 ID，或 `scan` |
| `COLLECT_CRON` | 五段 cron，默认 `*/5 * * * *` |
| `COLLECT_ON_BOOT` | `1` 则 `collect.py` 启动时先采一次 |

local key 不放 `.env`，和设备一一对应，写在 `smartlife.db` 的 `devices` 表。

## 拉取设备与 local key

```bash
set -a && source .env && set +a
.venv/bin/python -m tinytuya wizard -nocolor -yes \
  -key "$TUYA_API_KEY" -secret "$TUYA_API_SECRET" \
  -region "$TUYA_API_REGION" -device "$TUYA_DEVICE_ID"
```

成功后写入 `devices.json`、`tuya-raw.json`、`snapshot.json`。这些文件含 local key，不要提交。

云端返回 data center suspended 时，到 [iot.tuya.com](https://iot.tuya.com) 开通对应数据中心，或改 `TUYA_API_REGION`。

## 网络

发现广播必须从对应网卡发出。本机 TUN/代理会让默认 `tinytuya scan` 选错出口。扫描要绑定实际网卡，广播到该网段的广播地址。网卡、网段和设备地址见 `AGENTS.local.md`。

`-force` 逐 IP 扫描需要 `devices.json` 里已有 local key，否则 tinytuya 会关闭 force-scan。

## 当前设备

二氧化碳检测仪。地址、id 和 product 见 `AGENTS.local.md`。

| DPS | 含义 |
|---|---|
| 1 | `co2_state` |
| 2 | `co2_value`（ppm） |
| 18 | `temp_current`（°C） |
| 19 | `humidity_value`（%） |
| 15 | `battery_percentage` |

## 数据库

`init.sql` 建 `devices`、`readings`，并写入当前这台检测仪（含 local key）。该文件不提交。

```bash
sqlite3 smartlife.db < init.sql
```

`devices`：`id`、`name`、`address`、`version`、`product_id`、`category`、`mac`、`local_key`、`updated_at`。

`readings`：`device_id`、`ts`（UTC）、`co2_ppm`、`co2_state`、`temp_c`、`humidity`、`battery`、`raw_json`。

## 采集

`collect.py` 是常驻进程，按 `COLLECT_CRON` 对齐到整分钟。读 `devices` 里每一台，用本地协议取状态后写入 `readings`。失败、库不存在或没有设备时只追加 `collect.log`，不插入读数。醒来时若已错过该整点，记 `missed`，不补写。

```bash
.venv/bin/python collect.py
```

单次读取当前二氧化碳：

```bash
.venv/bin/python test_co2.py
```
