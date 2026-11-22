"""Contract and tag identity decisions."""
from .store import one,record

FORBIDDEN={'hazmat','live_animal'}

def booking(db,e,seq):
    bag=e['bag_id'];bid=e['booking_id']
    if one(db,'SELECT 1 FROM bag_tag WHERE bag_id=?',(bag,)):
        raise ValueError('bag_tag_already_assigned')
    if one(db,'SELECT 1 FROM booking WHERE booking_id=?',(bid,)):
        raise ValueError('booking_id_reused')
    items=set(e['items']);weight=float(e['weight_kg'])
    if not e['passenger'] or not e['destination'] or not e['flight'] or e['promised_at']<=e['checked_at']:
        raise ValueError('incomplete_booking')
    status='accepted';reason='contract_and_tag_match'
    if e.get('tag_passenger',e['passenger'])!=e['passenger']:
        status,reason='held','ticket_tag_passenger_mismatch'
    elif e.get('tag_destination',e['destination'])!=e['destination']:
        status,reason='held','ticket_tag_destination_mismatch'
    elif e.get('flight_destination',e['destination'])!=e['destination']:
        status,reason='held','flight_destination_mismatch'
    elif items&FORBIDDEN:status,reason='held','prohibited_item'
    elif weight<=0 or weight>32:status,reason='held','weight_outside_contract'
    db.execute('INSERT INTO booking VALUES(?,?,?,?,?,?,?,?)',
        (bid,e['passenger'],e['flight'],e['destination'],e['checked_at'],e['promised_at'],'active',1))
    db.execute('INSERT INTO bag_tag VALUES(?,?,?,?,?,?)',
        (bag,bid,','.join(sorted(items)),weight,status,1))
    record(db,bag,'baggage',e['event_id'],status,reason,seq)
    return {'status':status,'reason':reason}

def change(db,e,seq):
    bag=e['bag_id'];tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(bag,))
    if not tag:raise ValueError('unknown_bag')
    contract=one(db,'SELECT * FROM booking WHERE booking_id=?',(tag['booking_id'],))
    if e['version']!=contract['version']+1:raise ValueError('booking_version_conflict')
    if e.get('passenger',contract['passenger'])!=contract['passenger']:
        raise ValueError('passenger_identity_immutable')
    if e['action'] not in ('reroute','cancel'):raise ValueError('unknown_booking_action')
    if e['action']=='reroute' and not e.get('destination'):raise ValueError('destination_required')
    if e['action']=='cancel':
        db.execute("UPDATE booking SET status='cancelled',version=? WHERE booking_id=?",(e['version'],contract['booking_id']))
        status,reason='held','booking_cancelled'
    else:
        db.execute('UPDATE booking SET destination=?,version=? WHERE booking_id=?',
                   (e['destination'],e['version'],contract['booking_id']))
        status,reason='review','destination_rerouted'
    db.execute('UPDATE bag_tag SET status=?,tag_version=? WHERE bag_id=?',(status,e['version'],bag))
    record(db,bag,'baggage',e['event_id'],status,reason,seq)
    from .sortation import rejudge
    from .claims import rejudge as rejudge_claims
    rejudge(db,bag,seq,e['event_id']);rejudge_claims(db,bag,seq,e['event_id'])
    return {'status':status,'reason':reason}
