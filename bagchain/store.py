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
CREATE TABLE IF NOT EXISTS reclaim_zone(id TEXT PRIMARY KEY,carousel TEXT NOT NULL,capacity INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reclaim_tally(id TEXT PRIMARY KEY,zone_id TEXT NOT NULL REFERENCES reclaim_zone(id),at TEXT NOT NULL,entered INTEGER,exited INTEGER,raw TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reclaim_service(zone_id TEXT PRIMARY KEY,queue_count INTEGER NOT NULL,status TEXT NOT NULL);
-- Versioned wait evidence. The anonymous tally/count tables above are never altered:
-- voluntary ticket numbers and anonymous track ids live only in these separate tables.
CREATE TABLE IF NOT EXISTS reclaim_evidence_file(sha TEXT PRIMARY KEY,zone_id TEXT NOT NULL,kind TEXT NOT NULL,
  raw BLOB NOT NULL,imported_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reclaim_gate_calibration(zone_id TEXT NOT NULL,gate_id TEXT NOT NULL,version INTEGER NOT NULL,
  bias_lo_seconds INTEGER NOT NULL,bias_hi_seconds INTEGER NOT NULL,
  PRIMARY KEY(zone_id,gate_id,version));
CREATE TABLE IF NOT EXISTS reclaim_wait_certificate(cert_id TEXT NOT NULL,zone_id TEXT NOT NULL,version INTEGER NOT NULL,
  window_start TEXT,window_end TEXT,threshold_seconds INTEGER NOT NULL,
  lower_bound INTEGER NOT NULL,upper_bound INTEGER NOT NULL,observed_entered INTEGER NOT NULL,
  observed_exited INTEGER NOT NULL,status TEXT NOT NULL,problems TEXT NOT NULL,
  lower_pairing TEXT NOT NULL,upper_pairing TEXT NOT NULL,
  basis TEXT NOT NULL,sources TEXT NOT NULL,created_seq INTEGER NOT NULL,
  PRIMARY KEY(zone_id,version));
CREATE TABLE IF NOT EXISTS reclaim_ticket_voucher(voucher_id TEXT PRIMARY KEY,cert_id TEXT NOT NULL,zone_id TEXT NOT NULL,
  ticket_no TEXT NOT NULL,entry_at TEXT NOT NULL,exit_at TEXT NOT NULL,wait_seconds INTEGER NOT NULL,
  status TEXT NOT NULL,reason TEXT NOT NULL,entry_source_sha TEXT NOT NULL,exit_source_sha TEXT NOT NULL,
  version INTEGER NOT NULL);
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
