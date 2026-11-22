import json
from bagchain.carousel import import_zone,import_tally
from bagchain.store import connect


def test_anonymous_tally_has_no_individual_waits(tmp_path):
    db=tmp_path/'bag.sqlite'
    zone=tmp_path/'zone.json';zone.write_text(json.dumps({'zone_id':'C1','carousel':'R1','capacity':5}))
    tally=tmp_path/'tally.json';tally.write_text(json.dumps({'tally_id':'T1','zone_id':'C1','at':'2026-11-22T08:00:00Z','entered':2,'exited':0}))
    import_zone(db,zone)
    assert import_tally(db,tally)['queue_count']==2
    with connect(db) as c:
        assert c.execute('SELECT status FROM reclaim_service WHERE zone_id=?',('C1',)).fetchone()[0]=='open'
