-- GreenTea NLV — TimescaleDB 初期スキーマ (store.py SCHEMAS と整合)
-- 各 stream を hypertable 化。時刻列でパーティション、圧縮ポリシで長期保管を効率化。
-- daemon の PgSink (実装は要望時) が INSERT する想定。NAS Parquet 解析だけなら不要。

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- 下り受信 (主 OWD ソース)
CREATE TABLE IF NOT EXISTS rx_dl (
  ts            TIMESTAMPTZ NOT NULL,          -- t_rpid_recv_unix を変換
  robot_id      INT,  hid_ip TEXT, hid_seq BIGINT, dl_seq BIGINT, aipc_seq BIGINT,
  cycle_count   BIGINT, corr_unix_time DOUBLE PRECISION,
  t_hid_rx_tsf_us BIGINT, t_hid_rx_esp_us BIGINT,
  t_hid_tx_tsf_us BIGINT, t_hid_tx_esp_us BIGINT,
  frame_size    INT, owd_dl_approx_us DOUBLE PRECISION,
  run_tag       TEXT
);
SELECT create_hypertable('rx_dl', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_rx_dl_robot ON rx_dl (robot_id, ts DESC);

-- sniffer air frame (高レート、干渉/air leg)
CREATE TABLE IF NOT EXISTS sniffer_frame (
  ts            TIMESTAMPTZ NOT NULL,
  rx_seq BIGINT, tsf_us BIGINT, rx_timestamp_us BIGINT,
  bb_format INT, rate INT, channel INT, rssi INT, sig_len INT, hdr_seq INT,
  src TEXT, dst TEXT, fc_lo INT, fc_hi INT, dropped_lo BIGINT,
  cycle_count BIGINT, robot_id INT, run_tag TEXT
);
SELECT create_hypertable('sniffer_frame', 'ts', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS idx_snf_robot ON sniffer_frame (robot_id, ts DESC);

-- PPS (同期精度)
CREATE TABLE IF NOT EXISTS pps_gpio (ts TIMESTAMPTZ NOT NULL, unix_assert DOUBLE PRECISION, sequence BIGINT, run_tag TEXT);
SELECT create_hypertable('pps_gpio', 'ts', if_not_exists => TRUE);
CREATE TABLE IF NOT EXISTS pps_uart (ts TIMESTAMPTZ NOT NULL, tsf_us BIGINT, esp_us BIGINT, run_tag TEXT);
SELECT create_hypertable('pps_uart', 'ts', if_not_exists => TRUE);

-- 圧縮ポリシ (7日より古いチャンクを圧縮)。長期大量データの容量削減。
ALTER TABLE sniffer_frame SET (timescaledb.compress, timescaledb.compress_segmentby = 'robot_id');
SELECT add_compression_policy('sniffer_frame', INTERVAL '7 days', if_not_exists => TRUE);
ALTER TABLE rx_dl SET (timescaledb.compress, timescaledb.compress_segmentby = 'robot_id');
SELECT add_compression_policy('rx_dl', INTERVAL '7 days', if_not_exists => TRUE);
