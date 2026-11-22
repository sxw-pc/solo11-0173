import json
from bagchain.belt import import_motor,import_sample
from bagchain.store import connect


def test_rms_only_alarm_is_provisional(tmp_path):
    db=tmp_path/'bag.sqlite'
    motor=tmp_path/'motor.json';motor.write_text(json.dumps({'motor_id':'M1','station':'SORT-A','rated_rpm':1200}))
    sample=tmp_path/'sample.json';sample.write_text(json.dumps({'sample_id':'S1','motor_id':'M1','sample_hz':200,'rms_g':0.5,'samples':[0,1,0,-1]}))
    import_motor(db,motor)
    assert import_sample(db,sample)['status']=='inspect'
    with connect(db) as c:
        assert c.execute('SELECT status FROM belt_maintenance WHERE motor_id=?',('M1',)).fetchone()[0]=='inspect'
