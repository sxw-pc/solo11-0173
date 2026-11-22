"""Baseline belt inspection from the current acceleration RMS."""


def advise(db,motor_id):
    sample=db.execute('SELECT * FROM belt_sample WHERE motor_id=? ORDER BY rowid DESC LIMIT 1',
                      (motor_id,)).fetchone()
    status='inspect' if sample['rms_g']>=0.4 else 'run'
    db.execute('INSERT INTO belt_maintenance VALUES(?,?,?) ON CONFLICT(motor_id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (motor_id,status,'rms_threshold'))
    return {'motor_id':motor_id,'status':status}
