"""Raw three-channel baggage screening profiles and scans.

The profile table keeps only the three-channel reference vectors per material
revision; the scan table keeps the original raw channels.  Neither table stores
a conclusion about the bag's contents: a single nearest reference is a
provisional label produced later by :mod:`bagchain.screening`, never proof of
what is inside the bag.
"""
import json
from .store import one,record


def profile(db,e,seq):
    if not one(db,'SELECT 1 FROM bag_tag WHERE bag_id=?',(e['bag_id'],)):
        raise ValueError('unknown_bag')
    channels=e['channels']
    if len(channels)!=3 or any(not isinstance(x,(int,float)) or x<0 for x in channels):
        raise ValueError('invalid_profile_channels')
    db.execute('INSERT INTO screen_profile VALUES(?,?,?,?,?)',
        (e['profile_id'],e['bag_id'],e['material'],json.dumps(channels),e['revision']))
    record(db,e['bag_id'],'screen',e['event_id'],'registered','reference_profile',seq)
    return {'status':'registered','reason':'reference_profile'}


def scan(db,e,seq):
    tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(e['bag_id'],))
    if not tag:raise ValueError('unknown_bag')
    channels=e['channels']
    if len(channels)!=3 or any(not isinstance(x,(int,float)) or x<0 for x in channels):
        raise ValueError('invalid_scan_channels')
    state='captured' if tag['status']=='accepted' else 'held'
    db.execute('INSERT INTO screen_scan VALUES(?,?,?,?,?)',
        (e['scan_id'],e['bag_id'],json.dumps(channels),e['seen_at'],state))
    record(db,e['bag_id'],'screen',e['event_id'],state,'raw_scan',seq)
    return {'status':state,'reason':'raw_scan'}
