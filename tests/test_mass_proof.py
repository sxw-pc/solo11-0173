"""Mass-proof acceptance: shared platform dynamics versus per-bag static reweigh."""
import json
from pathlib import Path

import pytest

from bagchain.report import report
from bagchain.scale import (import_accel,import_calibration,import_force,import_overload,
                            import_position,import_reweigh,import_scale,import_tare)
from bagchain.source import import_file
from bagchain.store import all_rows,connect,one

FIX=Path(__file__).resolve().parent.parent/'fixtures'
BUNDLE=json.loads((FIX/'dynamic-scale.json').read_text(encoding='utf-8'))


def stage(folder,name,doc):
    folder.mkdir(parents=True,exist_ok=True)
    f=folder/name;f.write_text(json.dumps(doc),encoding='utf-8');return f


def world(folder,patch_positions=None):
    """Identical shared state before supplementary evidence: declared, force, provisional."""
    db=folder/'bag.sqlite'
    import_file(db,'bag',stage(folder,'bookings.json',BUNDLE['bookings']))
    import_scale(db,stage(folder,'scale.json',BUNDLE['scale']))
    import_calibration(db,stage(folder,'calibration.json',BUNDLE['calibration']))
    import_tare(db,stage(folder,'tare.json',BUNDLE['tare']))
    for force in BUNDLE['forces']:import_force(db,stage(folder,force['sample_id']+'.json',force))
    for pos in BUNDLE['positions']:
        if patch_positions and pos['position_id'] in patch_positions:
            pos={**pos,**patch_positions[pos['position_id']]}
        import_position(db,stage(folder,pos['position_id']+'.json',pos))
    return db


def test_seed_baseline_1176n_remains_12kg_and_zero_fee(tmp_path):
    db=tmp_path/'bag.sqlite'
    import_scale(db,stage(tmp_path,'scale.json',{'scale_id':'W1','station':'CHECKIN-1'}))
    force={'sample_id':'F1','scale_id':'W1','bag_id':'BAG-1','at':'2026-11-22T08:00:00Z','force_n':117.6}
    assert import_force(db,stage(tmp_path,'force.json',force))['fee_cents']==0
    with connect(db) as c:
        assert abs(one(c,'SELECT * FROM excess_fee WHERE bag_id=?',('BAG-1',))['estimated_kg']-12)<1e-9
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-1',))['status']=='pending'
        assert one(c,'SELECT 1 AS x FROM fee_settlement WHERE bag_id=?',('BAG-1',)) is None
        ticket=one(c,'SELECT * FROM mass_ticket WHERE bag_id=?',('BAG-1',))
        assert ticket['reliable']==0 and ticket['confirmed_kg'] is None


def test_392n_two_worlds_diverge_only_with_acceleration_evidence(tmp_path):
    worlds={'static':('accel_world_a',40.0,34000,'rejected','held'),
            'lift':('accel_world_b',32.0,18000,'accepted','accepted')}
    pre={}
    for name,(key,kg,cents,status,tag) in worlds.items():
        db=world(tmp_path/name)
        with connect(db) as c:
            pre[name]=(one(c,'SELECT * FROM excess_fee WHERE bag_id=?',('BAG-X',)),
                       one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status'],
                       one(c,'SELECT COUNT(*) AS n FROM fee_settlement')['n'])
        import_accel(db,stage(tmp_path/name,'accel.json',BUNDLE[key]))
        with connect(db) as c:
            got=one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))
            assert abs(got['basis_kg']-kg)<1e-9 and got['fee_cents']==cents
            assert got['method']=='imu_reconstruct'
            acc=one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))
            assert acc['status']==status and acc['basis']==BUNDLE[key]['series_id']
            prov=one(c,'SELECT * FROM excess_fee WHERE bag_id=?',('BAG-X',))
            assert abs(prov['estimated_kg']-40.0)<1e-9 and prov['fee_cents']==34000
        assert report(db,'BAG-X')['bag']['status']==tag
        assert report(db,'BAG-X')['bag']['weight_kg']==32
        if name=='lift':
            with connect(db) as c:
                y=one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))
                assert abs(y['basis_kg']-25.0)<1e-9 and y['fee_cents']==4000
    assert pre['static'][0]['estimated_kg']==pre['lift'][0]['estimated_kg']==pytest.approx(40.0)
    assert pre['static'][0]['fee_cents']==pre['lift'][0]['fee_cents']==34000
    assert pre['static'][1]==pre['lift'][1]=='pending'
    assert pre['static'][2]==pre['lift'][2]==0


def test_shared_shift_acceleration_solved_once_and_not_per_bag(tmp_path):
    db=world(tmp_path/'w')
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        refs={r['bag_id']:r['evidence_ref'] for r in all_rows(c,'SELECT * FROM mass_ticket WHERE reliable=1')}
        assert refs=={'BAG-X':'IMU-W1-S1-LIFT','BAG-Y':'IMU-W1-S1-LIFT'}
    cheat={'series_id':'IMU-W1-S1-CHEAT','scale_id':'W1','shift':'SHIFT-1',
           'points':[['2026-11-22T08:04:00Z',0.0],['2026-11-22T08:08:00Z',0.0]]}
    with pytest.raises(ValueError,match='accel_series_overlap'):
        import_accel(db,stage(tmp_path/'w','cheat.json',cheat))
    later={'series_id':'IMU-W1-S2','scale_id':'W1','shift':'SHIFT-2',
           'points':[['2026-11-22T09:00:00Z',0.0],['2026-11-22T09:10:00Z',0.0]]}
    assert import_accel(db,stage(tmp_path/'w','later.json',later))['status']=='imported'


def test_tare_correction_not_counted_as_mass(tmp_path):
    folder=tmp_path/'w';db=folder/'bag.sqlite'
    import_scale(db,stage(folder,'scale.json',{'scale_id':'W9','station':'CHECKIN-9'}))
    import_calibration(db,stage(folder,'cal.json',{'cal_id':'CAL9','scale_id':'W9','batch':'B9',
                        'gain':1.0,'at':'2026-11-22T07:55:00Z'}))
    import_tare(db,stage(folder,'tare.json',{'tare_id':'T9','scale_id':'W9','at':'2026-11-22T07:56:00Z',
                        'tare_n':9.8,'drift_n':0.0,'reproducible':True}))
    import_force(db,stage(folder,'f.json',{'sample_id':'F9','scale_id':'W9','bag_id':'BAG-T',
                        'at':'2026-11-22T08:05:00Z','force_n':392.0}))
    import_position(db,stage(folder,'p.json',{'position_id':'P9','sample_id':'F9','bag_id':'BAG-T',
                        'at':'2026-11-22T08:05:00Z','corner_on_scale':True}))
    import_accel(db,stage(folder,'a.json',{'series_id':'IMU9','scale_id':'W9','shift':'S',
                        'points':[['2026-11-22T08:00:00Z',0.0],['2026-11-22T08:10:00Z',0.0]]}))
    with connect(db) as c:
        got=one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-T',))
        assert abs(got['basis_kg']-39.0)<1e-9 and got['fee_cents']==32000


def test_corner_off_scale_keeps_range_and_stops_auto_charge(tmp_path):
    db=world(tmp_path/'w',{'POS-X':{'corner_on_scale':False}})
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        assert one(c,'SELECT 1 AS x FROM fee_settlement WHERE bag_id=?',('BAG-X',)) is None
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status']=='pending'
        ticket=one(c,'SELECT * FROM mass_ticket WHERE bag_id=? ORDER BY version DESC',('BAG-X',))
        assert ticket['reliable']==0 and 'corner_off_scale' in ticket['issues']
        assert abs(ticket['feasible_min_kg']-32.0)<1e-9 and ticket['feasible_max_kg'] is None
        assert one(c,'SELECT * FROM excess_fee WHERE bag_id=?',('BAG-X',))['fee_cents']==34000
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))['fee_cents']==4000


def test_sensor_overload_saturation_keeps_lower_bound_only(tmp_path):
    db=world(tmp_path/'w')
    overload={'overload_id':'OVL-X','sample_id':'F-X','scale_id':'W1','at':'2026-11-22T08:05:00Z'}
    import_overload(db,stage(tmp_path/'w','ovl.json',overload))
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        assert one(c,'SELECT 1 AS x FROM fee_settlement WHERE bag_id=?',('BAG-X',)) is None
        ticket=one(c,'SELECT * FROM mass_ticket WHERE bag_id=? ORDER BY version DESC',('BAG-X',))
        assert 'force_saturated' in ticket['issues']
        assert abs(ticket['feasible_min_kg']-32.0)<1e-9 and ticket['feasible_max_kg'] is None
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status']=='pending'
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))['fee_cents']==4000


def test_unreproducible_tare_yields_bounded_range_without_charge(tmp_path):
    folder=tmp_path/'w';db=folder/'bag.sqlite'
    import_scale(db,stage(folder,'scale.json',{'scale_id':'W8','station':'CHECKIN-8'}))
    import_calibration(db,stage(folder,'cal.json',{'cal_id':'CAL8','scale_id':'W8','batch':'B8',
                        'gain':1.0,'at':'2026-11-22T07:55:00Z'}))
    import_tare(db,stage(folder,'tare.json',{'tare_id':'T8','scale_id':'W8','at':'2026-11-22T07:56:00Z',
                        'tare_n':0.0,'drift_n':4.9,'reproducible':False}))
    import_force(db,stage(folder,'f.json',{'sample_id':'F8','scale_id':'W8','bag_id':'BAG-U',
                        'at':'2026-11-22T08:05:00Z','force_n':392.0}))
    import_position(db,stage(folder,'p.json',{'position_id':'P8','sample_id':'F8','bag_id':'BAG-U',
                        'at':'2026-11-22T08:05:00Z','corner_on_scale':True}))
    import_accel(db,stage(folder,'a.json',{'series_id':'IMU8','scale_id':'W8','shift':'S',
                        'points':[['2026-11-22T08:00:00Z',0.0],['2026-11-22T08:10:00Z',0.0]]}))
    with connect(db) as c:
        assert one(c,'SELECT 1 AS x FROM fee_settlement WHERE bag_id=?',('BAG-U',)) is None
        ticket=one(c,'SELECT * FROM mass_ticket WHERE bag_id=? ORDER BY version DESC',('BAG-U',))
        assert 'tare_not_reproducible' in ticket['issues']
        assert abs(ticket['feasible_min_kg']-39.5)<1e-9 and abs(ticket['feasible_max_kg']-40.5)<1e-9


def test_imu_original_settles_whole_shift_batch(tmp_path):
    db=world(tmp_path/'w')
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))['fee_cents']==18000
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))['fee_cents']==4000
        decided={r['bag_id']:r['status'] for r in all_rows(c,'SELECT * FROM acceptance')}
        assert decided=={'BAG-X':'accepted','BAG-Y':'accepted'}


def test_static_reweigh_covers_single_bag_only(tmp_path):
    db=world(tmp_path/'w')
    import_reweigh(db,stage(tmp_path/'w','rw.json',BUNDLE['reweigh_x']))
    with connect(db) as c:
        x=one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))
        assert x['fee_cents']==18000 and x['method']=='static_reweigh'
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status']=='accepted'
        assert one(c,'SELECT 1 AS y FROM fee_settlement WHERE bag_id=?',('BAG-Y',)) is None
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-Y',))['status']=='pending'
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        rows=all_rows(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))
        assert len(rows)==1 and rows[0]['fee_cents']==18000 and rows[0]['method']=='static_reweigh'
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))['fee_cents']==4000


def test_scale_backfill_then_manual_reweigh_still_charges_once(tmp_path):
    db=world(tmp_path/'w')
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    import_reweigh(db,stage(tmp_path/'w','rw.json',BUNDLE['reweigh_x']))
    with connect(db) as c:
        rows=all_rows(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))
        assert len(rows)==1 and rows[0]['fee_cents']==18000 and rows[0]['method']=='imu_reconstruct'
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-Y',))['fee_cents']==4000


def test_repacked_reweigh_cannot_reverse_signed_acceptance(tmp_path):
    db=world(tmp_path/'w')
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_a']))
    import_reweigh(db,stage(tmp_path/'w','rw.json',BUNDLE['reweigh_x_repacked']))
    with connect(db) as c:
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status']=='rejected'
        assert one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))['fee_cents']==34000
        assert one(c,'SELECT * FROM static_reweigh WHERE reweigh_id=?',('RW-X-REPACK',)) is not None
    db2=world(tmp_path/'w2')
    import_reweigh(db2,stage(tmp_path/'w2','rw.json',BUNDLE['reweigh_x_repacked']))
    with connect(db2) as c:
        assert one(c,'SELECT 1 AS x FROM fee_settlement WHERE bag_id=?',('BAG-X',)) is None
        assert one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))['status']=='pending'
        ticket=one(c,'SELECT * FROM mass_ticket WHERE bag_id=? ORDER BY version DESC',('BAG-X',))
        assert 'seal_changed_reweigh_not_retroactive' in ticket['issues']


def test_version_chain_links_declared_force_provisional_ticket_and_decision(tmp_path):
    db=world(tmp_path/'w')
    import_accel(db,stage(tmp_path/'w','accel.json',BUNDLE['accel_world_b']))
    with connect(db) as c:
        declared=one(c,'SELECT * FROM bag_tag WHERE bag_id=?',('BAG-X',))
        assert declared['weight_kg']==32 and declared['tag_version']==1
        sample=one(c,'SELECT * FROM scale_sample WHERE bag_id=?',('BAG-X',))
        assert sample['force_n']==392.0
        prov=one(c,'SELECT * FROM excess_fee WHERE bag_id=?',('BAG-X',))
        assert abs(prov['estimated_kg']-40.0)<1e-9 and prov['fee_cents']==34000
        tickets=all_rows(c,'SELECT * FROM mass_ticket WHERE bag_id=? ORDER BY version',('BAG-X',))
        assert [t['version'] for t in tickets]==list(range(1,len(tickets)+1))
        final=tickets[-1]
        assert final['reliable']==1 and abs(final['confirmed_kg']-32.0)<1e-9
        settled=one(c,'SELECT * FROM fee_settlement WHERE bag_id=?',('BAG-X',))
        assert settled['ticket_version']==final['version'] and settled['fee_cents']==18000
        acc=one(c,'SELECT * FROM acceptance WHERE bag_id=?',('BAG-X',))
        assert acc['basis']==final['evidence_ref'] and acc['version']==1
        reasons=[d['reason'] for d in all_rows(
            c,"SELECT * FROM decision WHERE bag_id=? AND subsystem='baggage' ORDER BY id",('BAG-X',))]
        assert reasons==['contract_and_tag_match','measured_weight_within_contract']
