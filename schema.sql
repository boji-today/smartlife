CREATE TABLE IF NOT EXISTS devices (
  id TEXT PRIMARY KEY,
  name TEXT,
  address TEXT NOT NULL,
  version REAL NOT NULL,
  product_id TEXT,
  category TEXT,
  mac TEXT,
  local_key TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  device_id TEXT NOT NULL,
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

