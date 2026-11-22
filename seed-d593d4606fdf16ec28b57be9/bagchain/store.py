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
CREATE TABLE IF NOT EXISTS settlement(payment_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL,
  payer TEXT NOT NULL,amount_cents INTEGER NOT NULL,status TEXT NOT NULL,source_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS liability_share(claim_id TEXT NOT NULL,party TEXT NOT NULL,
  share_cents INTEGER NOT NULL,document TEXT NOT NULL,confirmed_at TEXT NOT NULL,source_sha TEXT NOT NULL,
  PRIMARY KEY(claim_id,party));
CREATE TABLE IF NOT EXISTS payment_intent(request_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL,
  purpose TEXT NOT NULL,payer TEXT NOT NULL,payee TEXT NOT NULL,amount_cents INTEGER NOT NULL,
  share_key TEXT NOT NULL,state TEXT NOT NULL,bank_status TEXT,debt_id TEXT,source_sha TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS party_debt(debt_id TEXT PRIMARY KEY,claim_id TEXT NOT NULL,
  debtor TEXT NOT NULL,creditor TEXT NOT NULL,amount_cents INTEGER NOT NULL,status TEXT NOT NULL,
  source TEXT NOT NULL);
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
