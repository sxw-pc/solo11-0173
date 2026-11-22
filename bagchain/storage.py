"""Provisional fit by front silhouette only."""
from .store import one,all_rows,record


def offer(db,e,seq):
    shape=one(db,'SELECT * FROM bag_silhouette WHERE id=?',(e['silhouette_id'],))
    if not shape or shape['bag_id']!=e['bag_id']:raise ValueError('unknown_silhouette')
    units=all_rows(db,'SELECT * FROM locker_unit WHERE width_cm>=? AND height_cm>=? ORDER BY depth_cm,id',
                   (shape['width_cm'],shape['height_cm']))
    unit=units[0] if units else None
    state='provisional' if unit else 'none'
    db.execute('INSERT INTO locker_offer VALUES(?,?,?,?,?,?)',
        (e['offer_id'],e['bag_id'],e['silhouette_id'],unit['id'] if unit else None,state,'front_projection_only'))
    record(db,e['bag_id'],'storage',e['event_id'],state,'front_projection_only',seq)
    return {'status':state,'reason':'front_projection_only'}
