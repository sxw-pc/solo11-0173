"""Evidence and observable conclusions for a baggage tag."""
from .settlement import bank_snapshot
from .store import all_rows,connect,one

def _clearing(db,bag,bank_path):
    """Clearing view: who really paid the passenger, what is unknown, what is owed.

    Customer receipt is derived from the external bank ledger (terminal
    ``transferred`` only), never from ``claim.status`` or local books alone.
    """
    claims=all_rows(db,'SELECT * FROM claim WHERE bag_id=? ORDER BY at,claim_id',(bag,))
    claim_ids={c['claim_id'] for c in claims}
    claimant={c['claim_id']:c['claimant'] for c in claims}
    intents=all_rows(db,'SELECT * FROM payment_intent WHERE claim_id IN (%s)'%
                     ','.join('?'*len(claim_ids)),tuple(claim_ids)) if claim_ids else []
    intent_by_id={i['request_id']:i for i in intents}
    debts=all_rows(db,'SELECT * FROM party_debt WHERE claim_id IN (%s) ORDER BY debt_id'%
                   ','.join('?'*len(claim_ids)),tuple(claim_ids)) if claim_ids else []
    shares=all_rows(db,'SELECT * FROM liability_share WHERE claim_id IN (%s) ORDER BY party'%
                    ','.join('?'*len(claim_ids)),tuple(claim_ids)) if claim_ids else []
    bank=bank_snapshot(bank_path)
    bank_by_id={r.get('payment_id'):r for r in bank}
    settled={s['payment_id']:s for s in
             all_rows(db,"SELECT * FROM settlement WHERE status='paid'")}
    received=[]
    for record in bank:
        if record.get('status')!='transferred' or record.get('claim_id') not in claim_ids:continue
        intent=intent_by_id.get(record['payment_id'])
        purpose=intent['purpose'] if intent else 'claim_principal'
        if purpose!='claim_principal':continue
        payee=record.get('payee') or (intent['payee'] if intent else claimant.get(record['claim_id']))
        received.append({'payment_id':record['payment_id'],'payer':record['payer'],
                         'payee':payee,'amount_cents':int(record['amount_cents'])})
    received.sort(key=lambda r:r['payment_id'])
    unknown=[]
    todos=[]
    for intent in intents:
        if intent['state'] not in ('reserved','sent'):continue
        record=bank_by_id.get(intent['request_id'])
        bank_status=record['status'] if record else 'absent'
        unknown.append({'payment_id':intent['request_id'],'purpose':intent['purpose'],
                        'payer':intent['payer'],'payee':intent['payee'],
                        'amount_cents':intent['amount_cents'],'state':intent['state'],
                        'bank_status':bank_status})
        if bank_status=='transferred' and intent['request_id'] not in settled:
            todos.append({'todo':'apply_bank_terminal','payment_id':intent['request_id']})
        else:
            todos.append({'todo':'confirm_bank_terminal','payment_id':intent['request_id'],
                          'bank_status':bank_status})
    for debt in debts:
        if debt['status']=='open':
            todos.append({'todo':'collect_party_share','debt_id':debt['debt_id'],
                          'debtor':debt['debtor'],'creditor':debt['creditor'],
                          'amount_cents':debt['amount_cents']})
    for c in claims:
        if c['status']=='compensable':continue
        paid=0
        for payment_id,row in settled.items():
            if row['claim_id']!=c['claim_id']:continue
            intent=intent_by_id.get(payment_id)
            if intent is None or intent['purpose']=='claim_principal':
                paid+=row['amount_cents']
        if paid>0:
            todos.append({'todo':'clawback_passenger','claim_id':c['claim_id'],
                          'from':c['claimant'],'amount_cents':paid})
    return {'liability_shares':shares,
            'customer_received':received,
            'customer_received_cents':sum(r['amount_cents'] for r in received),
            'bank_unknown':unknown,
            'party_debts':debts,
            'todos':todos}

def report(db_path,bag,bank_path=None):
    with connect(db_path) as db:
        tag=one(db,'SELECT * FROM bag_tag WHERE bag_id=?',(bag,))
        if not tag:raise ValueError('unknown_bag')
        book=one(db,'SELECT * FROM booking WHERE booking_id=?',(tag['booking_id'],))
        result={'bag':tag,'booking':book,
                'scans':all_rows(db,'SELECT * FROM scan WHERE bag_id=? ORDER BY seen_at,scan_id',(bag,)),
                'loads':all_rows(db,'SELECT * FROM load_manifest WHERE bag_id=? ORDER BY confirmed_at,load_id',(bag,)),
                'handoffs':all_rows(db,'SELECT * FROM handoff WHERE bag_id=? ORDER BY at,handoff_id',(bag,)),
                'arrivals':all_rows(db,'SELECT * FROM arrival WHERE bag_id=? ORDER BY at,arrival_id',(bag,)),
                'claims':all_rows(db,'SELECT * FROM claim WHERE bag_id=? ORDER BY at,claim_id',(bag,)),
                'settlements':all_rows(db,'SELECT s.* FROM settlement s JOIN claim c ON c.claim_id=s.claim_id WHERE c.bag_id=? ORDER BY s.payment_id',(bag,)),
                'decisions':all_rows(db,'SELECT * FROM decision WHERE bag_id=? ORDER BY id',(bag,)),
                'sources':all_rows(db,'SELECT DISTINCT source_sha FROM evidence WHERE bag_id=?',(bag,))}
        if bank_path is not None:
            result['clearing']=_clearing(db,bag,bank_path)
        return result
