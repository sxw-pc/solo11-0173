"""A queue-count projection without passenger-specific waiting times."""


def project(db,zone_id):
    zone=db.execute('SELECT * FROM reclaim_zone WHERE id=?',(zone_id,)).fetchone()
    rows=db.execute('SELECT * FROM reclaim_tally WHERE zone_id=? ORDER BY at,id',(zone_id,)).fetchall()
    count=0
    for row in rows:count+=row['entered']-row['exited']
    status='crowded' if count>=zone['capacity'] else 'open'
    db.execute('INSERT INTO reclaim_service VALUES(?,?,?) ON CONFLICT(zone_id) DO UPDATE SET queue_count=excluded.queue_count,status=excluded.status',
               (zone_id,count,status))
    return {'zone_id':zone_id,'queue_count':count,'status':status}
