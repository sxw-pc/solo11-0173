import json
from bagchain.scale import import_scale,import_force
from bagchain.store import connect


def test_force_fee_is_provisional(tmp_path):
    db=tmp_path/'bag.sqlite'
    scale=tmp_path/'scale.json';scale.write_text(json.dumps({'scale_id':'W1','station':'CHECKIN-1'}))
    force=tmp_path/'force.json';force.write_text(json.dumps({'sample_id':'F1','scale_id':'W1','bag_id':'BAG-1','at':'2026-11-22T08:00:00Z','force_n':117.6}))
    import_scale(db,scale)
    assert import_force(db,force)['fee_cents']==0
    with connect(db) as c:
        assert abs(c.execute('SELECT estimated_kg FROM excess_fee WHERE bag_id=?',('BAG-1',)).fetchone()[0]-12)<1e-9
