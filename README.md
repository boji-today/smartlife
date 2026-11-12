# smartlife

用 [tinytuya](https://github.com/jasonacox/tinytuya) 在局域网发现并读取 Tuya / Smart Life 设备，并用 `ha_probe.py` 只读查询 Home Assistant。

## 环境

```bash
python3 -m venv .venv
.venv/bin/pip install tinytuya
cp .env.example .env
```

`.env` 填写 Tuya IoT 云端凭据。local key 不放 `.env`，写在 `data/smartlife.db` 的 `devices` 表。`.env`、`data.sql`、`devices.json`、`tuya-raw.json`、`snapshot.json` 含密钥或 local key，不要提交。

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

## 拉取设备与 local key

```bash
set -a && source .env && set +a
.venv/bin/python -m tinytuya wizard -nocolor -yes \
  -key "$TUYA_API_KEY" -secret "$TUYA_API_SECRET" \
  -region "$TUYA_API_REGION" -device "$TUYA_DEVICE_ID"
```

成功后写入 `devices.json`、`tuya-raw.json`、`snapshot.json`。

云端返回 data center suspended 时，到 [iot.tuya.com](https://iot.tuya.com) 开通对应数据中心，或改 `TUYA_API_REGION`。

## 网络

发现广播必须从对应网卡发出。本机 TUN/代理会让默认 `tinytuya scan` 选错出口。扫描要绑定实际网卡，广播到该网段的广播地址。网卡、网段和设备标识见 `AGENTS.local.md`。

`-force` 逐 IP 扫描需要 `devices.json` 里已有 local key，否则 tinytuya 会关闭 force-scan。

## 数据库

`schema.sql` 提交仓库。`data.sql` 不提交。

```bash
mkdir -p data && cat schema.sql data.sql | sqlite3 data/smartlife.db
```

`devices` 以 `source` 与 `biz_id` 为唯一键，`active=0` 不采集。`readings` 按设备写入对应列，其余为空。列定义见 `schema.sql`。

## 采集

`collect.py` 按 `COLLECT_CRON` 常驻。只读 `active=1` 的行，涂鸦和 HA 并行，一路失败只记自己的 `collect.log`，不取消另一路。错过整点记 `missed`，不补写。失败、库不存在或没有设备时不插入读数。

```bash
.venv/bin/python collect.py
```

涂鸦固定 2 个线程，每台单独连接。品类 `co2bj` 写二氧化碳列，`kj` 写净化器列。

HA 在单独线程采集，不占用这 2 个线程。归属只认 device registry。`device_class` 对上的写入数值列，对不上的只进 `raw_json`。`ha_domains` 为 `[]` 或不是 JSON 数组时不插入。

`ha_event_selectors` 与 `ha_domains` 分开，默认 `[]` 不采集事件历史。事件原样写入 `activity_events`。人在传感器把间隔小于 600 秒的有人事件合成一段 `occupied`：`start_time` 是第一条事件时间，`end_time` 是最后一条加 600 秒。空白不是无人。查询用设备名，不再加这 600 秒。

`collect2.py` 是限时长连接探测。只读 `devices` 里 `source=tuya` 的第一行，保持 TCP，大约每 12 秒发心跳，把设备主动推送的 DPS 打到标准输出。不写 `readings`，不提供 HTTP。默认听 600 秒。与 `collect.py` 或容器不要同时连接同一台设备。断线重连、内存快照和落库尚未实现，见脚本顶部 TODO。

```bash
.venv/bin/python collect2.py
.venv/bin/python collect2.py 600
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
- `GET /activity_intervals?from=${__from}&to=${__to}`，可加 `name`、`kind`

`/readings` 的 `from` / `to` 为 Unix 毫秒或 UTC ISO8601（`Z` 或 `+00:00`），缺省最近 24 小时，比较前截断到整秒。`name` 对应 `devices.name`，`source` 对应 `readings.source`，缺省不过滤。响应按 `ts` 升序，不含 local key 与 `raw_json`。

`/activity_intervals` 的 `from` / `to` 同样接受 Unix 毫秒或 UTC ISO8601，但保留亚秒。缺省最近 24 小时。`name` 与 `kind` 缺省不过滤，传了则精确匹配。相交条件是 `start_time < to AND end_time > from`。

Grafana 与容器同在 `grafana` 网络时，Infinity 使用类型 JSON、Parser Backend、Format Time series。

```text
http://smartlife:8080/readings?from=${__from}&to=${__to}&name=<devices.name>&source=<tuya|ha>
```

## Home Assistant

`ha_probe.py` 只读探测，不调用服务，不落盘。

`sync_ha_devices.py` 用同样的只读请求。给已有 `source=ha` 写入 `room`，把人在传感器的名称改成 HA 设备名，并在缺行时插入门窗传感器。不插入其他设备。可重复执行。

```bash
.venv/bin/python ha_probe.py
.venv/bin/python sync_ha_devices.py
```
