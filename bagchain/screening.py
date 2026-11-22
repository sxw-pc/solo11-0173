"""Provisional nearest-material label for a single scan.

Only the reference profiles registered for the *same bag* participate, and the
result stays explicitly ``provisional``.  A nearest neighbour cannot establish
restricted-material content: when a composite reference is the midpoint of two
pure materials, two physically different packings share an identical label.
The bounded decomposition lives in :mod:`bagchain.material_unmix` and never
overwrites the label or the booking's declared items.
"""
import json
import math
from .store import one,all_rows,record


def label(db,e,seq):
    scan=one(db,'SELECT * FROM screen_scan WHERE scan_id=?',(e['scan_id'],))
    if not scan or scan['bag_id']!=e['bag_id']:raise ValueError('unknown_screen_scan')
    profiles=all_rows(db,
        'SELECT * FROM screen_profile WHERE bag_id=? ORDER BY rowid',(e['bag_id'],))
    if not profiles:raise ValueError('missing_screen_profile')
    values=json.loads(scan['channels_json'])
    best=None
    for profile in profiles:
        reference=json.loads(profile['channels_json'])
        distance=math.sqrt(sum((x-y)**2 for x,y in zip(values,reference)))
        if best is None or distance<best[0]:
            best=(distance,profile)
    distance,profile=best
    db.execute('INSERT INTO screen_label VALUES(?,?,?,?,?,?)',
        (e['label_id'],e['bag_id'],e['scan_id'],profile['material'],distance,'provisional'))
    record(db,e['bag_id'],'screen',e['event_id'],'provisional',profile['material'],seq)
    return {'status':'provisional','reason':profile['material']}
