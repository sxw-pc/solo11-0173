"""Import local conveyor motor samples with a provisional RMS alarm."""
import json
from pathlib import Path
from .store import connect


def import_motor(db_path,file):
    doc=json.loads(Path(file).read_text())
    with connect(db_path) as db,db:
        if not doc['motor_id'] or doc['rated_rpm']<=0:raise ValueError('invalid_motor')
        db.execute('INSERT INTO belt_motor VALUES(?,?,?)',
                   (doc['motor_id'],doc['station'],doc['rated_rpm']))
    return {'motor_id':doc['motor_id']}


def import_sample(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if not db.execute('SELECT 1 FROM belt_motor WHERE id=?',(doc['motor_id'],)).fetchone():
            raise ValueError('unknown_motor')
        if doc['sample_hz']<=0 or doc['rms_g']<0:raise ValueError('invalid_sample')
        db.execute('INSERT INTO belt_sample VALUES(?,?,?,?,?)',
                   (doc['sample_id'],doc['motor_id'],doc['sample_hz'],doc['rms_g'],raw))
        from .maintenance import advise
        return advise(db,doc['motor_id'])
