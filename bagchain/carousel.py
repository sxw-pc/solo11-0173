"""Collect anonymous counts at a baggage reclaim carousel."""
import json
from pathlib import Path
from .store import connect


def import_zone(db_path,file):
    doc=json.loads(Path(file).read_text())
    with connect(db_path) as db,db:
        if not doc['zone_id'] or doc['capacity']<=0:raise ValueError('invalid_zone')
        db.execute('INSERT INTO reclaim_zone VALUES(?,?,?)',
                   (doc['zone_id'],doc['carousel'],doc['capacity']))
    return {'zone_id':doc['zone_id']}


def import_tally(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if doc['entered']<0 or doc['exited']<0:raise ValueError('invalid_tally')
        if not db.execute('SELECT 1 FROM reclaim_zone WHERE id=?',(doc['zone_id'],)).fetchone():
            raise ValueError('unknown_zone')
        db.execute('INSERT INTO reclaim_tally VALUES(?,?,?,?,?,?)',
                   (doc['tally_id'],doc['zone_id'],doc['at'],doc['entered'],doc['exited'],raw))
        from .queue_service import project
        return project(db,doc['zone_id'])
