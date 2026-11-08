# smartlife

用 [tinytuya](https://github.com/jasonacox/tinytuya) 在局域网发现并读取 Tuya / Smart Life 设备，并用 `ha_probe.py` 只读查询 Home Assistant。

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
| `HTTP_PORT` | Infinity HTTP 端口，默认 `8080` |
| `HA_URL` | Home Assistant 实例根地址 |
| `HA_TOKEN` | 个人资料页创建的长期访问令牌 |

local key 不放 `.env`，和设备一一对应，写在 `data/smartlife.db` 的 `devices` 表。

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

二氧化碳检测仪，品类 `co2bj`，`source=tuya`。地址、`biz_id` 和 product 见 `AGENTS.local.md`。

| DPS | 列 | 含义 |
|---|---|---|
| 1 | `co2_state` | 状态 |
| 2 | `co2_ppm` | 二氧化碳（ppm） |
| 18 | `temp_c` | 温度（°C） |
| 19 | `humidity` | 湿度（%） |
| 15 | `battery` | 电量（%） |

Honeywell H Max，品类 `kj`，协议 `3.4`，`source=tuya`。地址、`biz_id` 和 product 见 `AGENTS.local.md`。

| DPS | code | 列 | 含义 |
|---|---|---|---|
| 1 | `switch` | `switch` | 开关，`0/1` |
| 2 | `pm25` | `pm25` | PM2.5，1–999 |
| 5 | `filter` | `filter_pct` | 滤芯使用率，0–100% |
| 21 | `air_quality` | `air_quality` | `good` / `medium` / `severe` |
| 22 | `fault` | `fault` | 故障位图，`e1` / `e2` / `e4` |

本地 DPS `4`、`101`、`102`、`103`、`104`、`105` 不在云端规范里，只留在 `raw_json`。

四台 `source=ha` 设备与涂鸦在同一 cron 采集：餐厅米家智能温湿度计3 mini、主卧室温湿度计写入温度、湿度和电量；餐厅米家智能除湿机 50L 只写入温度，湿度和电量为空；餐厅小米智能门铃3 只写入电量，温度和湿度为空。

## 数据库

`schema.sql` 建 `devices`、`readings` 及索引，提交仓库。`data.sql` 写入二氧化碳检测仪和 Honeywell H Max（含 local key），不提交。

```bash
mkdir -p data && cat schema.sql data.sql | sqlite3 data/smartlife.db
```

`devices`：自增 `id`。`source` 与 `biz_id` 组成唯一键。`biz_name` 是原名称，`name` 供 Infinity 过滤。`source` 为 `tuya` 或 `ha`。`address`、`version`、`local_key` 可空。

`readings`：`device_id` 是 `devices.id`，并冗余 `source`、`biz_id`。其余为 `ts`（UTC）、`co2_ppm`、`co2_state`、`temp_c`、`humidity`、`battery`、`switch`、`pm25`、`filter_pct`、`air_quality`、`fault`、`raw_json`。另一台设备的列为空。已有库在采集或查询时补列。

## 采集

`collect.py` 是常驻进程，按 `COLLECT_CRON` 对齐到整分钟。读完 `devices` 后同时启动涂鸦和 HA，主流程等两边结束。任一路异常只记自己的 `collect.log`，不取消另一路。

`source=tuya` 仍用 `collect_one()`，不加重试。`biz_id` 走本地协议。`co2bj` 写二氧化碳列，`kj` 写 H Max 列。写入时带上 `devices.id`、`source`、`biz_id`。进程内固定 2 个线程，多出的设备排队；每台单独连接，一台失败不影响另一台。

`source=ha` 在单独线程里采集，不占用这 2 个线程，也不调用 `GET /api/states`。一条 WebSocket 连续发送 `config/entity_registry/list` 和 `get_states`。连接失败或任一命令失败，整段会话重试一次。在内存中用 `entity_id` 对齐，只保留 registry 的 `device_id` 等于该行 `biz_id`、且域名为 `sensor` 的实体。本轮一条 sensor 都没有则不插入。至少一条时插入一行。`device_class` 为 `temperature`、`humidity`、`battery` 时，恰好一个且是数字才写入 `temp_c`、`humidity`、`battery`，否则该列为 null。同一类多于一个或不是数字时记日志，仍然插入。缺的列不记错误。不读 `humidifier`，`event` 不采集。数值按返回保存，整数保持整数。插入 `devices.id`、`source`、`biz_id`、`ts` 和上述三列，其余指标列为 null。`raw_json` 含该设备本轮全部 sensor 的 state。

失败、库不存在或没有设备时只追加 `collect.log`，不插入读数。醒来时若已错过该整点，记 `missed`，不补写。

```bash
.venv/bin/python collect.py
```

`collect2.py` 是限时长连接探测。只读 `devices` 里 `source=tuya` 的第一行，保持 TCP，大约每 12 秒发心跳，把设备主动推送的 DPS 打到标准输出。不写 `readings`，不提供 HTTP。默认听 600 秒。与 `collect.py` 或容器不要同时连接同一台设备。断线重连、内存快照和落库尚未实现，见脚本顶部 TODO。

```bash
.venv/bin/python collect2.py
.venv/bin/python collect2.py 600
```

单次读取当前二氧化碳：

```bash
.venv/bin/python test_co2.py
```

Docker：

```bash
docker compose -f compose.yml up -d --build
```

容器名 `smartlife`，接入已有的 `grafana` 网络。`env_file` 读 `.env`，并把 `HTTP_PORT` 传入容器，默认 `8080`。`./data` 挂到 `/app/data`，库文件为 `data/smartlife.db`，WAL 与库在同一目录。镜像入口是 `python -u collect.py`，不含密钥和数据库。查询通过该网络上的容器名访问。

## Infinity

同一进程提供：

- `GET /health`
- `GET /readings?from=${__from}&to=${__to}`，可加 `name`、`source`

`from` / `to` 为 Unix 毫秒或 UTC ISO8601（`Z` 或 `+00:00`），缺省最近 24 小时。`name` 对应 `devices.name`，`source` 对应 `readings.source`，缺省不过滤。响应按 `ts` 升序，含 `name`、`source`、`co2_ppm`、`co2_state`、`temp_c`、`humidity`、`battery`、`switch`、`pm25`、`filter_pct`、`air_quality`、`fault`。不含 `device_id`、`biz_id`、local key 与 `raw_json`。

Grafana 与容器同在 `grafana` 网络时，Infinity 使用类型 JSON、Parser Backend、Format Time series。URL：

```text
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=米家智能温湿度计3 mini&source=ha
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=温湿度计&source=ha
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=米家智能除湿机 50L&source=ha
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=小米智能门铃3&source=ha
```

门铃列：`ts` Time，`battery` Number，`name` `source` String。`temp_c` 与 `humidity` 为空。

已有数据：

```text
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=二氧化碳检测仪&source=tuya
```

列：`ts` Time，`co2_ppm` `temp_c` `humidity` `battery` Number，`name` `source` `co2_state` String。

```text
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=Honeywell H Max&source=tuya
```

列：`ts` Time，`pm25` `filter_pct` `fault` `switch` Number，`name` `source` `air_quality` String。

## Home Assistant

`ha_probe.py` 读 `.env`，以及 `devices` 里 `source=ha` 的行。`biz_id` 是 HA device id，输出标题用 `name`。只做 `GET /api/`、`GET /api/states`，以及 WebSocket 的 `config/area_registry/list`、`config/floor_registry/list`、`config/entity_registry/list`、`config/device_registry/list`。不调用服务，不落盘。

```bash
.venv/bin/python ha_probe.py
```
