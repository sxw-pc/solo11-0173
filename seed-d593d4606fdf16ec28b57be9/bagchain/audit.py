"""Rebuild current decisions from retained original files."""
import tempfile
from pathlib import Path
from .settlement import _verify_failure_receipt, bank_snapshot
from .source import import_file
from .store import all_rows,connect,digest

TABLES=('booking','bag_tag','scan','load_manifest','handoff','arrival','claim','decision','evidence','liability_share')

def _bank_reconcile(db_path,bank_path):
    """Recompute clearing consistency from originals and the external bank snapshot.

    Every terminal bank transfer must be booked exactly once locally, every
    local paid row must be backed by a bank terminal fact, one bank entry may
    only land on one claim, and locally recorded intent states must match the
    bank's terminal view.
    """
    anomalies=[]
    bank=bank_snapshot(bank_path)
    bank_by_id={}
    for record in bank:
        payment_id=record.get('payment_id')
        if payment_id in bank_by_id:
            anomalies.append({'kind':'bank_duplicate_request','payment_id':payment_id})
        bank_by_id[payment_id]=record
        if record.get('status')=='failed' and not _verify_failure_receipt(record):
            anomalies.append({'kind':'bank_failure_receipt_invalid','payment_id':payment_id})
    with connect(db_path) as db:
        settlements={s['payment_id']:s for s in all_rows(db,'SELECT * FROM settlement')}
        intents={i['request_id']:i for i in all_rows(db,'SELECT * FROM payment_intent')}
        claims={c['claim_id']:c for c in all_rows(db,'SELECT * FROM claim')}
    for payment_id,record in bank_by_id.items():
        if record.get('status')!='transferred':continue
        booked=settlements.get(payment_id)
        if not booked:
            anomalies.append({'kind':'bank_transferred_without_settlement','payment_id':payment_id})
        elif booked['claim_id']!=record.get('claim_id') or booked['amount_cents']!=int(record['amount_cents']):
            anomalies.append({'kind':'bank_settlement_mismatch','payment_id':payment_id})
    for payment_id,booked in settlements.items():
        if booked['status']!='paid':continue
        record=bank_by_id.get(payment_id)
        if not record or record.get('status')!='transferred':
            anomalies.append({'kind':'settlement_without_bank_transfer','payment_id':payment_id})
    for claim_id,claim in claims.items():
        total=0
        for record in bank_by_id.values():
            if record.get('status')!='transferred' or record.get('claim_id')!=claim_id:continue
            intent=intents.get(record['payment_id'])
            if intent is None or intent['purpose']=='claim_principal':
                total+=int(record['amount_cents'])
        if total>claim['amount_cents']:
            anomalies.append({'kind':'claim_overpaid','claim_id':claim_id,
                              'bank_transferred_cents':total,'claim_cents':claim['amount_cents']})
    for request_id,intent in intents.items():
        record=bank_by_id.get(request_id)
        if intent['state']=='settled' and (not record or record.get('status')!='transferred'):
            anomalies.append({'kind':'intent_settled_without_bank_transfer','payment_id':request_id})
        if intent['state']=='failed' and (not record or record.get('status')!='failed'):
            anomalies.append({'kind':'intent_failed_without_bank_receipt','payment_id':request_id})
        if (intent['state'] in ('reserved','sent') and record
                and record.get('status')=='transferred' and request_id not in settlements):
            anomalies.append({'kind':'bank_terminal_not_applied','payment_id':request_id})
    return anomalies

def audit(db_path,bank_path=None):
    with connect(db_path) as db:
        sources=[(r['kind'],bytes(r['raw']),r['sha']) for r in db.execute('SELECT kind,raw,sha FROM source_file ORDER BY rowid')]
        original={name:all_rows(db,f'SELECT * FROM {name} ORDER BY 1') for name in TABLES}
    with tempfile.TemporaryDirectory(prefix='bagchain-audit-') as folder:
        rebuilt=Path(folder)/'rebuilt.sqlite'
        for i,(kind,raw,sha) in enumerate(sources):
            if digest(raw)!=sha:return {'status':'source_corrupt','source_sha':sha}
            file=Path(folder)/f'{i}.json';file.write_bytes(raw)
            import_file(rebuilt,kind,file)
        with connect(rebuilt) as db:
            replay={name:all_rows(db,f'SELECT * FROM {name} ORDER BY 1') for name in TABLES}
    mismatches=[name for name in TABLES if original[name]!=replay[name]]
    anomalies=_bank_reconcile(db_path,bank_path) if bank_path is not None else []
    ok=not mismatches and not anomalies
    return {'status':'ok' if ok else 'divergent','source_count':len(sources),
            'mismatches':mismatches,'bank_anomalies':anomalies}
