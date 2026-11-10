CREATE TABLE IF NOT EXISTS devices (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_id TEXT NOT NULL,
  biz_name TEXT,
  name TEXT NOT NULL,
  source TEXT NOT NULL,
  address TEXT,
  version REAL,
  product_id TEXT,
  category TEXT,
  mac TEXT,
  local_key TEXT,
  updated_at TEXT NOT NULL,
  ha_domains TEXT, -- source=ha 的 domain 白名单；NULL 为 sensor、humidifier、climate
  UNIQUE (source, biz_id)
);

CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  co2_ppm INTEGER, -- 二氧化碳检测仪 DPS 2，ppm
  co2_state TEXT, -- 二氧化碳检测仪 DPS 1
  temp_c INTEGER, -- 二氧化碳检测仪 DPS 18，°C
  humidity INTEGER, -- 二氧化碳检测仪 DPS 19，%
  battery INTEGER, -- 二氧化碳检测仪 DPS 15，%
  switch INTEGER, -- H Max DPS 1，开关，0/1
  pm25 INTEGER, -- H Max DPS 2，PM2.5，1–999
  filter_pct INTEGER, -- H Max DPS 5，滤芯使用率 %，0–100
  air_quality TEXT, -- H Max DPS 21，good/medium/severe
  fault INTEGER, -- H Max DPS 22，故障位图 e1/e2/e4
  raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_id, ts);
