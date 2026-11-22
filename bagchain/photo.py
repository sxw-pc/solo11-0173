"""Capture checked bag photos and provisional edge measurements."""
import json
from pathlib import Path
from .store import connect


def import_photo(db_path,file):
    raw=Path(file).read_text();doc=json.loads(raw)
    with connect(db_path) as db,db:
        if doc['stage'] not in ('checkin','arrival') or not 0<=doc['edge_score']<=1:
            raise ValueError('invalid_photo')
        db.execute('INSERT INTO bag_photo VALUES(?,?,?,?,?,?)',
                   (doc['photo_id'],doc['bag_id'],doc['stage'],doc['at'],doc['edge_score'],raw))
        from .damage import assess
        return assess(db,doc['bag_id'])
