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
CREATE TABLE IF NOT EXISTS screen_profile(profile_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,material TEXT NOT NULL,channels_json TEXT NOT NULL,revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS screen_scan(scan_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,channels_json TEXT NOT NULL,seen_at TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS screen_label(label_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,scan_id TEXT NOT NULL,material TEXT NOT NULL,distance REAL NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unmix_batch(batch_id TEXT PRIMARY KEY,bag_id TEXT NOT NULL,scan_id TEXT NOT NULL,
  reference_revision INTEGER NOT NULL,calibration_json TEXT NOT NULL,total_volume REAL NOT NULL,
  occluded_volume REAL NOT NULL,created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS unmix_voxel(voxel_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,bag_id TEXT NOT NULL,
  channels_json TEXT NOT NULL,thickness_json TEXT NOT NULL,occluded INTEGER NOT NULL,volume REAL NOT NULL,
  inspectable INTEGER NOT NULL,position TEXT NOT NULL,r_fraction_lo REAL,r_fraction_hi REAL,
  residual REAL NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unmix_bag_result(result_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,bag_id TEXT NOT NULL,
  route TEXT NOT NULL,result_version INTEGER NOT NULL,r_fraction_lo REAL,r_fraction_hi REAL,
  confirmed_volume REAL NOT NULL,total_volume REAL NOT NULL,occluded_volume REAL NOT NULL,
  unexplained_volume REAL NOT NULL,rescan_positions_json TEXT NOT NULL,state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS unmix_followup(followup_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,bag_id TEXT NOT NULL,
  route TEXT NOT NULL,device_id TEXT NOT NULL,calibration_version TEXT NOT NULL,evidence_json TEXT NOT NULL,
  coverage_json TEXT NOT NULL,created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS manual_open(open_id TEXT PRIMARY KEY,batch_id TEXT NOT NULL,bag_id TEXT NOT NULL,
  at TEXT NOT NULL,actors_json TEXT NOT NULL,findings_json TEXT NOT NULL,created_seq INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS decision(id INTEGER PRIMARY KEY,bag_id TEXT NOT NULL,subsystem TEXT NOT NULL,
  event_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created_seq INTEGER NOT NULL);
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
