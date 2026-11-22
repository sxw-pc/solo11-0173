"""Temporary locker dimensions and front-facing bag silhouettes."""
from .store import one,record


def unit(db,e,seq):
    if min(e['width_cm'],e['height_cm'],e['depth_cm'])<=0:raise ValueError('invalid_locker_dimensions')
    db.execute('INSERT INTO locker_unit VALUES(?,?,?,?,?)',
        (e['unit_id'],e['width_cm'],e['height_cm'],e['depth_cm'],e['zone']))
    record(db,e['bag_id'],'locker',e['event_id'],'registered','locker_unit',seq)
    return {'status':'registered','reason':'locker_unit'}


def silhouette(db,e,seq):
    if not one(db,'SELECT 1 FROM bag_tag WHERE bag_id=?',(e['bag_id'],)):
        raise ValueError('unknown_bag')
    if min(e['width_cm'],e['height_cm'])<=0:raise ValueError('invalid_silhouette')
    db.execute('INSERT INTO bag_silhouette VALUES(?,?,?,?,?)',
        (e['silhouette_id'],e['bag_id'],e['width_cm'],e['height_cm'],e['seen_at']))
    record(db,e['bag_id'],'locker',e['event_id'],'captured','front_silhouette',seq)
    return {'status':'captured','reason':'front_silhouette'}
