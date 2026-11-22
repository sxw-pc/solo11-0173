"""Capture a check-in load cell's force rather than an asserted bag mass."""
import json
from pathlib import Path
from .store import connect


def import_scale(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not doc['scale_id'] or not doc['station']:raise ValueError('invalid_scale')
        db.execute('INSERT INTO checkin_scale VALUES(?,?,?)',
                   (doc['scale_id'],doc['station'],raw))
    return {'scale_id':doc['scale_id']}


def import_force(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not db.execute('SELECT 1 FROM checkin_scale WHERE id=?',(doc['scale_id'],)).fetchone():
            raise ValueError('unknown_scale')
        if doc['force_n']<0:raise ValueError('invalid_force')
        db.execute('INSERT INTO scale_sample VALUES(?,?,?,?,?,?)',
                   (doc['sample_id'],doc['scale_id'],doc['bag_id'],doc['at'],doc['force_n'],raw))
        from .weight_fee import advise
        return advise(db,doc['bag_id'])
