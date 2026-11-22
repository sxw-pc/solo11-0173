"""Small local mock of claim payment to an external bank ledger."""
import json
from pathlib import Path
from .store import connect, digest, one


def pay(db_path, bank_path, request_file, fail_after_bank=False):
    raw = Path(request_file).read_bytes()
    event = json.loads(raw)
    with connect(db_path) as db:
        claim = one(db, 'SELECT * FROM claim WHERE claim_id=?', (event['claim_id'],))
    if not claim or claim['status'] != 'compensable':
        raise ValueError('claim_not_compensable')
    amount = int(event['amount_cents'])
    if amount <= 0 or amount > claim['amount_cents']:
        raise ValueError('amount_outside_claim')
    bank_file = Path(bank_path)
    bank_file.parent.mkdir(parents=True, exist_ok=True)
    ledger = json.loads(bank_file.read_text(encoding='utf-8')) if bank_file.exists() else []
    ledger.append({'payment_id': event['payment_id'], 'claim_id': event['claim_id'],
                   'payer': event['payer'], 'amount_cents': amount, 'status': 'transferred'})
    bank_file.write_text(json.dumps(ledger, sort_keys=True), encoding='utf-8')
    if fail_after_bank:
        raise RuntimeError('connection_lost_after_bank_transfer')
    with connect(db_path) as db, db:
        db.execute('INSERT INTO settlement VALUES(?,?,?,?,?,?)',
                   (event['payment_id'], event['claim_id'], event['payer'], amount,
                    'paid', digest(raw)))
    return {'payment_id': event['payment_id'], 'status': 'paid'}
