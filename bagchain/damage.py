"""Baseline damage assessment from before/after edge score difference."""


def assess(db,bag_id):
    before=db.execute("SELECT * FROM bag_photo WHERE bag_id=? AND stage='checkin' ORDER BY at DESC LIMIT 1",(bag_id,)).fetchone()
    after=db.execute("SELECT * FROM bag_photo WHERE bag_id=? AND stage='arrival' ORDER BY at DESC LIMIT 1",(bag_id,)).fetchone()
    if not before or not after:return {'bag_id':bag_id,'status':'await_pair'}
    delta=after['edge_score']-before['edge_score']
    status='damage_review' if delta>=0.3 else 'no_change'
    db.execute('INSERT INTO damage_assessment VALUES(?,?,?) ON CONFLICT(bag_id) DO UPDATE SET status=excluded.status,delta_score=excluded.delta_score',
               (bag_id,status,delta))
    return {'bag_id':bag_id,'status':status,'delta_score':delta}
