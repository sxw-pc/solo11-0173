"""Provisional nearest-material label for a single scan."""
import json,math
from .store import one,all_rows,record


def label(db,e,seq):
    scan=one(db,'SELECT * FROM screen_scan WHERE scan_id=?',(e['scan_id'],))
    if not scan or scan['bag_id']!=e['bag_id']:raise ValueError('unknown_screen_scan')
    profiles=all_rows(db,'SELECT * FROM screen_profile WHERE bag_id=?',(e['bag_id'],))
    if not profiles:raise ValueError('missing_screen_profile')
    values=json.loads(scan['channels_json'])
    best=min(profiles,key=lambda p:sum((x-y)**2 for x,y in zip(values,json.loads(p['channels_json']))))
    distance=math.sqrt(sum((x-y)**2 for x,y in zip(values,json.loads(best['channels_json']))))
    db.execute('INSERT INTO screen_label VALUES(?,?,?,?,?,?)',
        (e['label_id'],e['bag_id'],e['scan_id'],best['material'],distance,'provisional'))
    record(db,e['bag_id'],'screen',e['event_id'],'provisional',best['material'],seq)
    return {'status':'provisional','reason':best['material']}
