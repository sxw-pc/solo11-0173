"""A queue-count projection without passenger-specific waiting times.

The projection remains the live head-count control only.  Occluded door pages
carry ``NULL`` deltas and contribute nothing to the count rather than being
treated as zero people; reset pages restart the running count.  This table is
never enriched with ticket numbers or track identifiers.
"""
import json


def project(db, zone_id):
    zone = db.execute('SELECT * FROM reclaim_zone WHERE id=?', (zone_id,)).fetchone()
    rows = db.execute('SELECT * FROM reclaim_tally WHERE zone_id=? ORDER BY at,id',
                      (zone_id,)).fetchall()
    count = 0
    for row in rows:
        if row['entered'] is None or row['exited'] is None:
            continue  # blind page: no trustworthy count delta
        if json.loads(row['raw']).get('reset'):
            count = 0
        count += row['entered'] - row['exited']
    status = 'crowded' if count >= zone['capacity'] else 'open'
    db.execute('INSERT INTO reclaim_service VALUES(?,?,?) ON CONFLICT(zone_id) DO UPDATE SET queue_count=excluded.queue_count,status=excluded.status',
               (zone_id, count, status))
    return {'zone_id': zone_id, 'queue_count': count, 'status': status}
