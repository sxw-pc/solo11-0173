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
  event_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS belt_motor(id TEXT PRIMARY KEY,station TEXT NOT NULL,rated_rpm INTEGER NOT NULL,
  time_base_id TEXT);
CREATE TABLE IF NOT EXISTS belt_sample(id TEXT PRIMARY KEY,motor_id TEXT NOT NULL REFERENCES belt_motor(id),
  sample_hz INTEGER NOT NULL,rms_g REAL NOT NULL,raw TEXT NOT NULL,time_base_id TEXT,
  seq_index INTEGER,tach_pulses TEXT,aa_filter TEXT,channel_gain REAL,source_sha TEXT,n_points INTEGER);
CREATE TABLE IF NOT EXISTS belt_segment(segment_id TEXT PRIMARY KEY,sample_id TEXT NOT NULL,
  time_base_id TEXT NOT NULL,seq_index INTEGER NOT NULL,sample_hz INTEGER NOT NULL,n_points INTEGER NOT NULL,
  aa_filter TEXT,channel_gain REAL,dropped_frames INTEGER NOT NULL,saturated INTEGER NOT NULL,
  samples TEXT NOT NULL,UNIQUE(sample_id,seq_index));
CREATE TABLE IF NOT EXISTS belt_evidence(evidence_id TEXT PRIMARY KEY,motor_id TEXT NOT NULL,sample_id TEXT NOT NULL,
  kind TEXT NOT NULL,source_sha TEXT NOT NULL,payload TEXT NOT NULL,recorded_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS belt_advice_version(advice_id TEXT PRIMARY KEY,motor_id TEXT NOT NULL,sample_id TEXT NOT NULL,
  version INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,rms_g REAL NOT NULL,created_seq INTEGER NOT NULL,
  UNIQUE(motor_id,version));
CREATE TABLE IF NOT EXISTS belt_work_order(order_id TEXT PRIMARY KEY,motor_id TEXT NOT NULL,station TEXT NOT NULL,
  order_type TEXT NOT NULL,band TEXT,evidence_sample_id TEXT,review_kind TEXT,evidence_sha TEXT,
  downtime_window TEXT,warranty_position TEXT,reason TEXT NOT NULL,created_at TEXT NOT NULL,version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS belt_review(review_id TEXT PRIMARY KEY,motor_id TEXT NOT NULL,sample_id TEXT NOT NULL,
  review_kind TEXT NOT NULL,evidence_ref TEXT NOT NULL,resolution TEXT NOT NULL,warranty_position TEXT NOT NULL,
  attributes TEXT NOT NULL,created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS belt_maintenance(motor_id TEXT PRIMARY KEY,status TEXT NOT NULL,reason TEXT NOT NULL);
"""

def digest(raw):return hashlib.sha256(raw).hexdigest()

def canonical(obj):return json.dumps(obj,sort_keys=True,ensure_ascii=False,separators=(',',':'))

@contextmanager
def connect(path):
    target=Path(path);target.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(target);db.row_factory=sqlite3.Row
    try:
        db.executescript(SCHEMA)
        yield db
    finally:db.close()

def one(db,sql,args=()):
    row=db.execute(sql,args).fetchone();return dict(row) if row else None

def all_rows(db,sql,args=()):return [dict(r) for r in db.execute(sql,args)]

def record(db,bag,subsystem,event,status,reason,seq):
    db.execute('INSERT INTO decision(bag_id,subsystem,event_id,status,reason,created_seq) VALUES(?,?,?,?,?,?)',
               (bag,subsystem,event,status,reason,seq))
