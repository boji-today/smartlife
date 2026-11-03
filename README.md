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
