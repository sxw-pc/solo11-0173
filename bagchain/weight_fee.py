"""Provisional overweight estimate and the one confirmed settlement per bag."""
from .store import one

FREE_KG=23.0
RATE_CENTS_PER_KG=2000

def fee_for_kg(kg):return round(max(0.0,kg-FREE_KG)*RATE_CENTS_PER_KG)

def advise(db,bag_id):
    """Provisional charge from force divided by nominal gravity; kept as history only."""
    row=db.execute('SELECT * FROM scale_sample WHERE bag_id=? ORDER BY at DESC LIMIT 1',
                   (bag_id,)).fetchone()
    mass=row['force_n']/9.8
    cents=fee_for_kg(mass)
    db.execute('INSERT INTO excess_fee VALUES(?,?,?) ON CONFLICT(bag_id) DO UPDATE SET estimated_kg=excluded.estimated_kg,fee_cents=excluded.fee_cents',
               (bag_id,mass,cents))
    return {'bag_id':bag_id,'estimated_kg':mass,'fee_cents':cents}

def settle(db,bag_id,confirmed_kg,method,ticket_version,at):
    """Publish the confirmed overweight charge once; later evidence never bills the bag twice."""
    old=one(db,'SELECT * FROM fee_settlement WHERE bag_id=?',(bag_id,))
    if old:return dict(old),False
    cents=fee_for_kg(confirmed_kg)
    db.execute('INSERT INTO fee_settlement VALUES(?,?,?,?,?,?)',
               (bag_id,cents,confirmed_kg,method,ticket_version,at))
    return {'bag_id':bag_id,'fee_cents':cents,'basis_kg':confirmed_kg,'method':method,
            'ticket_version':ticket_version,'settled_at':at},True
