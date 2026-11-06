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
  UNIQUE (source, biz_id)
);

CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id INTEGER NOT NULL,
  source TEXT NOT NULL,
  biz_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  co2_ppm INTEGER,
  co2_state TEXT,
  temp_c INTEGER,
  humidity INTEGER,
  battery INTEGER,
  raw_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
CREATE INDEX IF NOT EXISTS idx_readings_device_ts ON readings(device_id, ts);
