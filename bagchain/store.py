"""SQLite identity, evidence, and materialized business state."""
import hashlib
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS evidence(event_id TEXT PRIMARY KEY,kind TEXT NOT NULL,bag_id TEXT NOT NULL,
  source_sha TEXT NOT NULL,payload TEXT NOT NULL,received_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS source_file(sha TEXT PRIMARY KEY,kind TEXT NOT NULL,raw BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS booking(booking_id TEXT PRIMARY KEY,passenger TEXT NOT NULL,flight TEXT NOT NULL,
  destination TEXT NOT NULL,checked_at TEXT NOT NULL,promised_at TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS bag_tag(bag_id TEXT PRIMARY KEY,booking_id TEXT NOT NULL REFERENCES booking(booking_id),
  declared_items TEXT NOT NULL,weight_kg REAL NOT NULL,status TEXT NOT NULL,tag_version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS scan(scan_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,station TEXT NOT NULL,
  flight TEXT NOT NULL,seen_at TEXT NOT NULL,actor TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS load_manifest(load_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,flight TEXT NOT NULL,
  position TEXT NOT NULL,confirmed_at TEXT NOT NULL,loader TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS handoff(handoff_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,from_station TEXT NOT NULL,
  to_station TEXT NOT NULL,at TEXT NOT NULL,from_actor TEXT NOT NULL,to_actor TEXT NOT NULL,
  status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS arrival(arrival_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,airport TEXT NOT NULL,
  at TEXT NOT NULL,handler TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim(claim_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,claimant TEXT NOT NULL,
  kind TEXT NOT NULL,at TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,amount_cents INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,bag_id TEXT NOT NULL,subsystem TEXT NOT NULL,
  event_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created_seq INTEGER NOT NULL,
  provenance TEXT);
CREATE TABLE IF NOT EXISTS reader_batch(batch_sha TEXT NOT NULL,policy TEXT NOT NULL,raw BLOB NOT NULL,
  sort_source_sha TEXT,created_seq INTEGER NOT NULL,PRIMARY KEY(batch_sha,policy));
CREATE TABLE IF NOT EXISTS reader_calibration(id INTEGER PRIMARY KEY,batch_sha TEXT NOT NULL,policy TEXT NOT NULL,
  reader_id TEXT NOT NULL,restart_seq INTEGER NOT NULL,offset_s REAL NOT NULL,drift_s_per_s REAL NOT NULL,
  beacon_frame_ids TEXT NOT NULL,epoch_start_device TEXT NOT NULL,epoch_end_device TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_frame(frame_id TEXT NOT NULL,batch_sha TEXT NOT NULL,policy TEXT NOT NULL,
  reader_id TEXT,frame_type TEXT NOT NULL,bag_id TEXT,epc TEXT,gate TEXT,corrected_at TEXT,
  received_at TEXT,rssi_db REAL,zone TEXT,crc_ok INTEGER,usable INTEGER NOT NULL,role TEXT NOT NULL,
  reason TEXT NOT NULL,evidence_json TEXT NOT NULL,PRIMARY KEY(batch_sha,policy,frame_id));
CREATE TABLE IF NOT EXISTS reader_hypothesis(id INTEGER PRIMARY KEY,batch_sha TEXT NOT NULL,policy TEXT NOT NULL,
  hypothesis_no INTEGER NOT NULL,bag_id TEXT NOT NULL,gate TEXT,feasible INTEGER NOT NULL,
  unique_solution INTEGER NOT NULL,score REAL NOT NULL,rank_no INTEGER NOT NULL,
  assignment_json TEXT NOT NULL,reasons_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reader_decision(id INTEGER PRIMARY KEY,batch_sha TEXT NOT NULL,policy TEXT NOT NULL,
  bag_id TEXT NOT NULL,outcome TEXT NOT NULL,gate TEXT,flight TEXT,station TEXT,actor TEXT,seen_at TEXT,
  scan_event_id TEXT,load_event_id TEXT,reason TEXT NOT NULL,anchors_json TEXT NOT NULL,
  decisive_frame_ids TEXT NOT NULL,missing_anchor TEXT);
"""

def digest(raw):return hashlib.sha256(raw).hexdigest()

def canonical(obj):return json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':'))

@contextmanager
def connect(path):
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(target);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        # 旧库升级：decision 表补 provenance 列，保留既有审计数据。
        cols={r[1] for r in db.execute('PRAGMA table_info(decision)')}
        if 'provenance' not in cols:
            db.execute('ALTER TABLE decision ADD COLUMN provenance TEXT')
        yield db
    finally:db.close()

def one(db,sql,args=()):
    row=db.execute(sql,args).fetchone();return dict(row) if row else None

def all_rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args)]

def record(db,bag,subsystem,event,status,reason,seq,provenance=None):
    if isinstance(provenance,(dict,list)):provenance=canonical(provenance)
    db.execute('INSERT INTO decision(bag_id,subsystem,event_id,status,reason,created_seq,provenance) VALUES(?,?,?,?,?,?,?)',
               (bag,subsystem,event,status,reason,seq,provenance))
