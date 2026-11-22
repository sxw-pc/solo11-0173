import json
from bagchain.photo import import_photo
from bagchain.store import connect


def test_score_change_is_provisional(tmp_path):
    db=tmp_path/'bag.sqlite'
    def send(name,stage,score):
        file=tmp_path/f'{name}.json'
        file.write_text(json.dumps({'photo_id':name,'bag_id':'BAG-1','stage':stage,
            'at':f'2026-11-22T08:0{0 if stage=="checkin" else 5}:00Z','edge_score':score}))
        return import_photo(db,file)
    assert send('B','checkin',0.2)['status']=='await_pair'
    assert send('A','arrival',0.8)['status']=='damage_review'
    with connect(db) as c:
        assert abs(c.execute('SELECT delta_score FROM damage_assessment WHERE bag_id=?',('BAG-1',)).fetchone()[0]-0.6)<1e-9
