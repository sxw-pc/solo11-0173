"""Provisional overweight charge from force divided by nominal gravity."""


def advise(db,bag_id):
    row=db.execute('SELECT * FROM scale_sample WHERE bag_id=? ORDER BY at DESC LIMIT 1',
                   (bag_id,)).fetchone()
    mass=row['force_n']/9.8
    cents=round(max(0,mass-23)*2000)
    db.execute('INSERT INTO excess_fee VALUES(?,?,?) ON CONFLICT(bag_id) DO UPDATE SET estimated_kg=excluded.estimated_kg,fee_cents=excluded.fee_cents',
               (bag_id,mass,cents))
    return {'bag_id':bag_id,'estimated_kg':mass,'fee_cents':cents}
