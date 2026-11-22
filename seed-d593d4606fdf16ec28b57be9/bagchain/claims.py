"""Passenger receipt, trace obligation, and compensation decisions."""
from .store import all_rows,one,record

def _first_arrival(db,bag):
    # Delivery time is the first confirmed arrival: later evidence of an
    # earlier actual arrival rejudges lateness-based decisions.
    return one(db,"SELECT * FROM arrival WHERE bag_id=? AND status='confirmed' ORDER BY at,arrival_id LIMIT 1",(bag,))

def _decision(db,bag,claimant,kind,at,amount):
    tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(bag,))
    if not tag:raise ValueError('unknown_bag')
    book=one(db,'SELECT * FROM booking WHERE booking_id=?',(tag['booking_id'],))
    arrival=_first_arrival(db,bag)
    if claimant!=book['passenger']:return 'held','claimant_not_authorized'
    if book['status']!='active':return 'disputed','booking_cancelled'
    if kind=='pickup':
        if not arrival:return 'held','arrival_not_proven'
        if at<arrival['at']:return 'held','pickup_before_arrival'
        if one(db,"SELECT 1 FROM claim WHERE bag_id=? AND kind='pickup' AND status='settled'",(bag,)):
            return 'held','already_collected'
        return 'settled','passenger_collection_confirmed'
    if kind=='late':
        if at<=book['promised_at']:return 'held','claim_before_promise'
        if not one(db,"SELECT 1 FROM scan WHERE bag_id=? AND status='accepted'",(bag,)):
            return 'held','no_accepted_transport_evidence'
        if arrival:
            if arrival['at']<=book['promised_at']:return 'held','delivered_within_promise'
            if amount<=0 or amount>50000:return 'held','amount_outside_contract_cap'
            return 'compensable','late_arrival_confirmed'
        return 'trace','delivery_trace_required'
    if kind=='damage':
        if not arrival:return 'held','arrival_not_proven'
        if amount<=0 or amount>50000:return 'held','amount_outside_contract_cap'
        return 'review','independent_damage_review_required'
    raise ValueError('invalid_claim_kind')

def claim(db,e,seq):
    if one(db,'SELECT 1 FROM claim WHERE claim_id=?',(e['claim_id'],)):
        raise ValueError('claim_id_conflict')
    bag=e['bag_id'];kind=e['claim_type'];amount=int(e.get('amount_cents',0))
    status,reason=_decision(db,bag,e['claimant'],kind,e['at'],amount)
    if kind=='damage' and status=='review':
        if e.get('independent_witness') and e['independent_witness']!=e['claimant'] and e.get('damage_proof'):
            status,reason='compensable','independent_damage_and_cap_confirmed'
    db.execute('INSERT INTO claim VALUES(?,?,?,?,?,?,?,?)',
               (e['claim_id'],bag,e['claimant'],kind,e['at'],status,reason,amount))
    record(db,bag,'claim',e['event_id'],status,reason,seq)
    return {'status':status,'reason':reason}

def liability_share(db,e,seq):
    """Record one party's signed liability share for a claim (source document)."""
    target=one(db,'SELECT * FROM claim WHERE claim_id=?',(e['claim_id'],))
    if not target:raise ValueError('unknown_claim')
    party=e.get('party');share=int(e['share_cents'])
    if not party or share<=0:raise ValueError('invalid_liability_share')
    if not e.get('document'):raise ValueError('liability_document_required')
    if one(db,'SELECT 1 FROM liability_share WHERE claim_id=? AND party=?',(e['claim_id'],party)):
        raise ValueError('liability_share_conflict')
    signed=all_rows(db,'SELECT share_cents FROM liability_share WHERE claim_id=?',(e['claim_id'],))
    if share+sum(row['share_cents'] for row in signed)>target['amount_cents']:
        raise ValueError('liability_shares_exceed_claim')
    source=one(db,'SELECT source_sha FROM evidence WHERE event_id=?',(e['event_id'],))
    db.execute('INSERT INTO liability_share VALUES(?,?,?,?,?,?)',
               (e['claim_id'],party,share,e['document'],e['confirmed_at'],source['source_sha']))
    record(db,e['bag_id'],'claim',e['event_id'],'confirmed','liability_share_signed',seq)
    return {'status':'confirmed','reason':'liability_share_signed'}

def rejudge(db,bag,seq,trigger):
    for item in all_rows(db,'SELECT * FROM claim WHERE bag_id=? ORDER BY at,claim_id',(bag,)):
        if item['status']=='settled':
            arrival=one(db,"SELECT 1 FROM arrival WHERE bag_id=? AND status='confirmed'",(bag,))
            if arrival:continue
            db.execute("UPDATE claim SET status='disputed',reason='arrival_evidence_changed' WHERE claim_id=?",(item['claim_id'],))
            record(db,bag,'claim',trigger,'disputed','settled_pickup_under_review',seq)
            continue
        status,reason=_decision(db,bag,item['claimant'],item['kind'],item['at'],item['amount_cents'])
        if item['kind']=='damage' and item['status']=='compensable' and status=='review':continue
        if status!=item['status'] or reason!=item['reason']:
            db.execute('UPDATE claim SET status=?,reason=? WHERE claim_id=?',(status,reason,item['claim_id']))
            record(db,bag,'claim',trigger,status,'rejudged:'+reason,seq)
            if item['status']=='compensable' and status!='compensable':
                from .settlement import on_claim_reversed
                on_claim_reversed(db,item['claim_id'])
