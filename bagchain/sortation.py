"""Custody transfer and flight movement decisions."""
from .store import all_rows,one,record

def _contract(db,bag):
    tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(bag,))
    if not tag:raise ValueError('unknown_bag')
    booking=one(db,'SELECT * FROM booking WHERE booking_id=?',(tag['booking_id'],))
    return tag,booking

def scan(db,e,seq):
    bag=e['bag_id'];tag,book=_contract(db,bag)
    if one(db,'SELECT 1 FROM scan WHERE scan_id=?',(e['scan_id'],)):
        raise ValueError('scan_id_conflict')
    status,reason='accepted','flight_and_tag_match'
    if book['status']!='active' or tag['status']=='held':status,reason='held','booking_or_tag_inactive'
    elif e['flight']!=book['flight']:status,reason='held','wrong_flight'
    elif not e['actor']:status,reason='held','scanner_identity_missing'
    elif e['seen_at']<book['checked_at']:status,reason='held','scan_before_checkin'
    else:
        last=one(db,"SELECT * FROM scan WHERE bag_id=? AND status='accepted' ORDER BY seen_at DESC LIMIT 1",(bag,))
        if last and e['seen_at']<last['seen_at']:status,reason='held','scan_time_reversed'
    db.execute('INSERT INTO scan VALUES(?,?,?,?,?,?,?,?)',
               (e['scan_id'],bag,e['station'],e['flight'],e['seen_at'],e['actor'],status,reason))
    record(db,bag,'sortation',e['event_id'],status,reason,seq)
    return {'status':status,'reason':reason}

def handoff(db,e,seq):
    bag=e['bag_id'];_contract(db,bag)
    if one(db,'SELECT 1 FROM handoff WHERE handoff_id=?',(e['handoff_id'],)):
        raise ValueError('handoff_id_conflict')
    status,reason='accepted','dual_custody_transfer'
    scan=one(db,"SELECT * FROM scan WHERE bag_id=? AND station=? AND status='accepted' ORDER BY seen_at DESC LIMIT 1",
             (bag,e['from_station']))
    load=one(db,"SELECT * FROM load_manifest WHERE bag_id=? AND status='confirmed' ORDER BY confirmed_at DESC LIMIT 1",(bag,))
    if not scan:status,reason='held','missing_origin_scan'
    elif not load:status,reason='held','missing_load_confirmation'
    elif e['at']<load['confirmed_at']:status,reason='held','handoff_before_load'
    elif e['from_actor']==e['to_actor'] or not e['from_actor'] or not e['to_actor']:
        status,reason='held','custody_requires_independent_signers'
    elif e['at']<scan['seen_at']:status,reason='held','handoff_before_scan'
    elif e['from_station']==e['to_station']:status,reason='held','handoff_no_station_change'
    db.execute('INSERT INTO handoff VALUES(?,?,?,?,?,?,?,?,?)',
               (e['handoff_id'],bag,e['from_station'],e['to_station'],e['at'],e['from_actor'],e['to_actor'],status,reason))
    record(db,bag,'sortation',e['event_id'],status,reason,seq)
    return {'status':status,'reason':reason}

def load(db,e,seq):
    bag=e['bag_id'];tag,book=_contract(db,bag)
    if one(db,'SELECT 1 FROM load_manifest WHERE load_id=?',(e['load_id'],)):
        raise ValueError('load_id_conflict')
    scan=one(db,"SELECT * FROM scan WHERE bag_id=? AND status='accepted' ORDER BY seen_at DESC LIMIT 1",(bag,))
    status,reason='confirmed','load_position_committed'
    if not scan:status,reason='held','scan_not_accepted'
    elif e['flight']!=book['flight'] or tag['status']=='held':status,reason='held','load_contract_mismatch'
    elif e['confirmed_at']<scan['seen_at']:status,reason='held','load_before_scan'
    elif not e['position'] or not e['loader']:status,reason='held','load_evidence_incomplete'
    db.execute('INSERT INTO load_manifest VALUES(?,?,?,?,?,?,?,?)',
               (e['load_id'],bag,e['flight'],e['position'],e['confirmed_at'],e['loader'],status,reason))
    record(db,bag,'sortation',e['event_id'],status,reason,seq)
    return {'status':status,'reason':reason}

def arrival(db,e,seq):
    bag=e['bag_id'];_,book=_contract(db,bag)
    if one(db,'SELECT 1 FROM arrival WHERE arrival_id=?',(e['arrival_id'],)):
        raise ValueError('arrival_id_conflict')
    status,reason='confirmed','arrival_with_custody'
    transfer=one(db,"SELECT * FROM handoff WHERE bag_id=? AND to_station=? AND status='accepted' ORDER BY at DESC LIMIT 1",(bag,e['airport']))
    if e['airport']!=book['destination']:status,reason='held','arrival_wrong_destination'
    elif not transfer:status,reason='held','custody_gap'
    elif e['at']<transfer['at']:status,reason='held','arrival_before_handoff'
    elif not e['handler']:status,reason='held','handler_missing'
    db.execute('INSERT INTO arrival VALUES(?,?,?,?,?,?,?)',
               (e['arrival_id'],bag,e['airport'],e['at'],e['handler'],status,reason))
    record(db,bag,'sortation',e['event_id'],status,reason,seq)
    from .claims import rejudge as rejudge_claims
    rejudge_claims(db,bag,seq,e['event_id'])
    return {'status':status,'reason':reason}

def rejudge(db,bag,seq,trigger):
    _,book=_contract(db,bag)
    scans=all_rows(db,'SELECT * FROM scan WHERE bag_id=? ORDER BY seen_at,scan_id',(bag,))
    for item in scans:
        if item['status']=='accepted' and (book['status']!='active' or item['flight']!=book['flight']):
            db.execute("UPDATE scan SET status='disputed',reason='booking_changed' WHERE scan_id=?",(item['scan_id'],))
            record(db,bag,'sortation',trigger,'disputed','scan_booking_version_changed',seq)
    for transfer in all_rows(db,"SELECT * FROM handoff WHERE bag_id=? AND status='accepted'",(bag,)):
        source=one(db,"SELECT 1 FROM scan WHERE bag_id=? AND station=? AND status='accepted' AND seen_at<=?",
                   (bag,transfer['from_station'],transfer['at']))
        if not source or transfer['to_station']!=book['destination'] or book['status']!='active':
            db.execute("UPDATE handoff SET status='disputed',reason='upstream_changed' WHERE handoff_id=?",(transfer['handoff_id'],))
            record(db,bag,'sortation',trigger,'disputed','handoff_upstream_changed',seq)
    for load in all_rows(db,"SELECT * FROM load_manifest WHERE bag_id=? AND status='confirmed'",(bag,)):
        if book['status']!='active' or load['flight']!=book['flight']:
            db.execute("UPDATE load_manifest SET status='disputed',reason='booking_changed' WHERE load_id=?",(load['load_id'],))
            record(db,bag,'sortation',trigger,'disputed','load_booking_version_changed',seq)
    for item in all_rows(db,"SELECT * FROM arrival WHERE bag_id=? AND status='confirmed'",(bag,)):
        custody=one(db,"SELECT 1 FROM handoff WHERE bag_id=? AND to_station=? AND status='accepted' AND at<=?",
                    (bag,item['airport'],item['at']))
        if item['airport']!=book['destination'] or not custody or book['status']!='active':
            db.execute("UPDATE arrival SET status='disputed',reason='upstream_changed' WHERE arrival_id=?",(item['arrival_id'],))
            record(db,bag,'sortation',trigger,'disputed','arrival_upstream_changed',seq)
