"""Small local mock of claim payment to an external bank ledger.

This module keeps the original one-shot ``pay`` (bank file first, SQLite
second, no atomic commit across the two stores) and adds a recoverable
clearing protocol for airline/insurance fronted compensation.

Design anchors of the recoverable protocol:

* Stable request identity: the ``payment_id`` of a payment request file is
  the same key in both stores — ``payment_intent.request_id`` in SQLite and
  the mock bank record in ``bank.json``.  That bank record, keyed by the
  stable identity, is the recovery anchor after any crash.
* Occupation: every request first occupies a specific unpaid share of its
  claim (``share_key`` = ``principal:<claim>`` for the passenger principal or
  ``recovery:<debt>`` for one inter-party debt).  While an intent is
  ``reserved``/``sent`` its share stays occupied: no renumbering, no route
  switch, no release while the bank outcome is unknown.
* Bank terminal vs acceptance: the bank first records ``accepted`` (request
  received, money not moved).  Only the terminal state ``transferred`` books
  cash to the customer; terminal ``failed`` releases the occupation and only
  with a verifiable bank failure receipt.
* Purpose separation: the passenger principal and the inter-party recovery
  are different purposes booked separately — the fronting party's advance is
  one customer receipt, the resulting 80/120 recovery is a separate debt,
  never a second passenger payment.
"""
import json
from pathlib import Path
from .store import all_rows, canonical, connect, digest, one


# ---------------------------------------------------------------------------
# Original one-shot payment (kept unchanged: it demonstrates the cross-store
# failure — bank.json is written before the SQLite settlement insert and the
# two stores cannot commit atomically).
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Mock bank: an append-only external ledger file.  Records carry the stable
# request identity and move accepted -> transferred|failed (terminal).
# ---------------------------------------------------------------------------
BANK_ID = 'mock-bank'
TERMINAL = ('transferred', 'failed')


def _bank_load(bank_path):
    file = Path(bank_path)
    return json.loads(file.read_text(encoding='utf-8')) if file.exists() else []


def _bank_save(bank_path, ledger):
    file = Path(bank_path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(json.dumps(ledger, sort_keys=True), encoding='utf-8')


def bank_snapshot(bank_path):
    """External bank statement: every request record the bank knows about."""
    return _bank_load(bank_path)


def bank_query(bank_path, request_id):
    """Look up one stable request identity in the bank ledger."""
    for record in _bank_load(bank_path):
        if record.get('payment_id') == request_id:
            return record
    return None


def bank_submit(bank_path, record, fail_after_write=False):
    """Submit a transfer request; the bank records it as accepted (not terminal).

    Idempotent on the stable identity: resubmitting the same request content
    returns the existing record, conflicting content is rejected, so one bank
    entry can only ever belong to one claim.  ``fail_after_write`` simulates
    the response being lost after the bank durably accepted the request.
    """
    ledger = _bank_load(bank_path)
    for existing in ledger:
        if existing.get('payment_id') == record['payment_id']:
            same = (existing.get('claim_id') == record['claim_id']
                    and existing.get('payer') == record['payer']
                    and existing.get('payee') == record.get('payee')
                    and int(existing.get('amount_cents', 0)) == int(record['amount_cents']))
            if not same:
                raise ValueError('bank_request_conflict')
            return existing
    accepted = {**record, 'status': 'accepted'}
    ledger.append(accepted)
    _bank_save(bank_path, ledger)
    if fail_after_write:
        raise RuntimeError('connection_lost_after_bank_accept')
    return accepted


def bank_failure_receipt(record):
    """Verifiable failure receipt: the bank's signed statement of a terminal failure."""
    return digest(canonical({'bank': BANK_ID, 'payment_id': record['payment_id'],
                             'claim_id': record['claim_id'],
                             'amount_cents': int(record['amount_cents']),
                             'outcome': 'failed'}).encode('utf-8'))


def bank_finalize(bank_path, request_id, outcome, receipt=None):
    """Bank-side completion: move an accepted request to a terminal state.

    Terminal states are immutable; repeating the same outcome is a no-op.
    A failed outcome always carries the verifiable failure receipt.
    """
    ledger = _bank_load(bank_path)
    for record in ledger:
        if record.get('payment_id') == request_id:
            if record['status'] in TERMINAL:
                if record['status'] == outcome:
                    return record
                raise ValueError('bank_terminal_immutable')
            if outcome == 'transferred':
                record['status'] = 'transferred'
            elif outcome == 'failed':
                record['status'] = 'failed'
                record['receipt'] = receipt or bank_failure_receipt(record)
            else:
                raise ValueError('unknown_bank_outcome')
            _bank_save(bank_path, ledger)
            return record
    raise ValueError('bank_request_unknown')


def _verify_failure_receipt(record):
    return record.get('receipt') == bank_failure_receipt(record)


# ---------------------------------------------------------------------------
# Recoverable clearing protocol.
# ---------------------------------------------------------------------------
def route_plan(shares, fronter, principal_cents):
    """Derive the authorized route from signed liability shares.

    A route is: one liable party fronts the whole principal to the passenger,
    every other liable party then owes the fronter its own share.  Routes are
    derived from the share table (one per eligible fronter), never from
    payer-string branching.
    """
    parties = {share['party'] for share in shares}
    if fronter not in parties:
        raise ValueError('payer_not_liable')
    if sum(share['share_cents'] for share in shares) != principal_cents:
        raise ValueError('liability_shares_incomplete')
    return [{'debtor': share['party'], 'creditor': fronter, 'amount_cents': share['share_cents']}
            for share in shares if share['party'] != fronter]


def _principal_paid(db, claim_id):
    """Principal already booked as paid for a claim (any payment generation)."""
    rows = all_rows(db, "SELECT s.amount_cents, i.purpose FROM settlement s"
                        " LEFT JOIN payment_intent i ON i.request_id=s.payment_id"
                        " WHERE s.claim_id=? AND s.status='paid'", (claim_id,))
    return sum(row['amount_cents'] for row in rows if row['purpose'] in (None, 'claim_principal'))


def _get_or_create_intent(db, request, sha):
    request_id = request['payment_id']
    existing = one(db, 'SELECT * FROM payment_intent WHERE request_id=?', (request_id,))
    if existing:
        if existing['source_sha'] != sha:
            raise ValueError('request_id_conflict')
        if existing['state'] == 'cancelled':
            # Withdrawn when the claim left compensable; never executable again.
            raise ValueError('claim_not_compensable')
        return existing
    claim_id = request['claim_id']
    claim = one(db, 'SELECT * FROM claim WHERE claim_id=?', (claim_id,))
    if not claim:
        raise ValueError('unknown_claim')
    if claim['status'] != 'compensable':
        raise ValueError('claim_not_compensable')
    payer = request['payer']
    amount = int(request['amount_cents'])
    payee = request.get('payee')
    purpose = request.get('purpose')
    if payee is None or payee == claim['claimant']:
        if purpose not in (None, 'claim_principal'):
            raise ValueError('purpose_payee_mismatch')
        purpose, payee, debt_id = 'claim_principal', claim['claimant'], None
        shares = all_rows(db, 'SELECT * FROM liability_share WHERE claim_id=?', (claim_id,))
        if not shares:
            raise ValueError('liability_shares_required')
        route_plan(shares, payer, claim['amount_cents'])
        remaining = claim['amount_cents'] - _principal_paid(db, claim_id)
        if remaining <= 0:
            raise ValueError('principal_already_settled')
        if amount <= 0 or amount != remaining:
            raise ValueError('amount_outside_remaining_principal')
        share_key = f'principal:{claim_id}'
    else:
        if purpose not in (None, 'party_recovery'):
            raise ValueError('purpose_payee_mismatch')
        purpose = 'party_recovery'
        debt = one(db, "SELECT * FROM party_debt WHERE claim_id=? AND debtor=? AND creditor=?"
                       " AND status='open'", (claim_id, payer, payee))
        if not debt:
            raise ValueError('no_open_debt')
        if amount <= 0 or amount != debt['amount_cents']:
            raise ValueError('amount_outside_debt')
        debt_id = debt['debt_id']
        share_key = f'recovery:{debt_id}'
    occupied = one(db, "SELECT 1 FROM payment_intent WHERE share_key=? AND state IN ('reserved','sent')",
                   (share_key,))
    if occupied:
        raise ValueError('share_occupied')
    db.execute('INSERT INTO payment_intent VALUES(?,?,?,?,?,?,?,?,?,?,?)',
               (request_id, claim_id, purpose, payer, payee, amount, share_key,
                'reserved', None, debt_id, sha))
    return one(db, 'SELECT * FROM payment_intent WHERE request_id=?', (request_id,))


def _apply_transferred(db, intent):
    """Book a terminal bank transfer exactly once (duplicate callbacks add no cash)."""
    if not one(db, 'SELECT 1 FROM settlement WHERE payment_id=?', (intent['request_id'],)):
        db.execute('INSERT INTO settlement VALUES(?,?,?,?,?,?)',
                   (intent['request_id'], intent['claim_id'], intent['payer'],
                    intent['amount_cents'], 'paid', intent['source_sha']))
    db.execute("UPDATE payment_intent SET state='settled',bank_status='transferred'"
               " WHERE request_id=?", (intent['request_id'],))
    if intent['purpose'] == 'claim_principal':
        claim = one(db, 'SELECT * FROM claim WHERE claim_id=?', (intent['claim_id'],))
        if claim and claim['status'] == 'compensable':
            shares = all_rows(db, 'SELECT * FROM liability_share WHERE claim_id=?', (intent['claim_id'],))
            for debt in route_plan(shares, intent['payer'], claim['amount_cents']):
                debt_id = f"DEBT:{intent['claim_id']}:{debt['debtor']}->{debt['creditor']}"
                if not one(db, 'SELECT 1 FROM party_debt WHERE debt_id=?', (debt_id,)):
                    db.execute('INSERT INTO party_debt VALUES(?,?,?,?,?,?,?)',
                               (debt_id, intent['claim_id'], debt['debtor'], debt['creditor'],
                                debt['amount_cents'], 'open', f"fronted:{intent['request_id']}"))
    else:
        db.execute("UPDATE party_debt SET status='settled' WHERE debt_id=?", (intent['debt_id'],))


def _observe(db, bank_path, request_id):
    """Reconcile one intent with the bank record — the external recovery anchor."""
    intent = one(db, 'SELECT * FROM payment_intent WHERE request_id=?', (request_id,))
    record = bank_query(bank_path, request_id)
    if record is None:
        return {'payment_id': request_id, 'status': intent['state'], 'bank_status': 'absent'}
    if record.get('claim_id') != intent['claim_id'] or int(record.get('amount_cents', 0)) != intent['amount_cents']:
        return {'payment_id': request_id, 'status': intent['state'], 'bank_status': 'conflict'}
    status = record['status']
    db.execute('UPDATE payment_intent SET bank_status=? WHERE request_id=?', (status, request_id))
    if status == 'accepted':
        if intent['state'] == 'reserved':
            db.execute("UPDATE payment_intent SET state='sent' WHERE request_id=?", (request_id,))
    elif status == 'transferred':
        _apply_transferred(db, intent)
    elif status == 'failed':
        if _verify_failure_receipt(record):
            db.execute("UPDATE payment_intent SET state='failed' WHERE request_id=?"
                       " AND state IN ('reserved','sent')", (request_id,))
        else:
            state = one(db, 'SELECT state FROM payment_intent WHERE request_id=?', (request_id,))['state']
            return {'payment_id': request_id, 'status': state, 'bank_status': 'failed_receipt_invalid'}
    else:
        raise ValueError('unknown_bank_status')
    state = one(db, 'SELECT state FROM payment_intent WHERE request_id=?', (request_id,))['state']
    return {'payment_id': request_id, 'status': 'paid' if state == 'settled' else state,
            'bank_status': status}


def settle(db_path, bank_path, request_file, fail_after_bank=False):
    """Execute one payment request recoverably.

    The intent (stable identity + share occupation) is committed to SQLite
    before the bank is touched; the bank record is then adopted or submitted
    and its current state observed.  Crashing between the two stores is
    recovered by re-calling with the same request file or by ``settle_sync``.
    """
    raw = Path(request_file).read_bytes()
    request = json.loads(raw)
    sha = digest(raw)
    with connect(db_path) as db, db:
        intent = _get_or_create_intent(db, request, sha)
    record = bank_query(bank_path, request['payment_id'])
    if record is None:
        if intent['state'] == 'reserved':
            bank_submit(bank_path, {'payment_id': request['payment_id'],
                                    'claim_id': intent['claim_id'], 'payer': intent['payer'],
                                    'payee': intent['payee'], 'amount_cents': intent['amount_cents']},
                        fail_after_write=fail_after_bank)
        elif intent['state'] == 'sent':
            # The request left for the bank but the bank has no record of it:
            # never renumber or blindly resubmit — the share stays occupied.
            raise RuntimeError('bank_record_missing_for_sent_request')
    with connect(db_path) as db, db:
        return _observe(db, bank_path, request['payment_id'])


def settle_sync(db_path, bank_path):
    """Recovery sweep: query the bank for every non-terminal intent and apply it.

    Covers the crash window where the bank accepted (or even transferred) but
    the local store never learned it.  Terminal bank facts are applied even to
    withdrawn (cancelled) intents — bank facts are never erased locally.
    """
    transitions = []
    with connect(db_path) as db, db:
        pending = all_rows(db, "SELECT request_id, state FROM payment_intent"
                               " WHERE state IN ('reserved','sent','cancelled') ORDER BY request_id")
        for item in pending:
            result = _observe(db, bank_path, item['request_id'])
            after = one(db, 'SELECT state FROM payment_intent WHERE request_id=?',
                        (item['request_id'],))['state']
            if after != item['state']:
                transitions.append({'payment_id': item['request_id'],
                                    'from': item['state'], 'to': after,
                                    'bank_status': result['bank_status']})
    return {'synced': transitions}


def on_claim_reversed(db, claim_id):
    """A claim left ``compensable``: stop what is unpaid, keep bank facts.

    Reserved (never bank-confirmed) intents are withdrawn and open inter-party
    debts are voided.  Sent intents stay occupied — the bank may still hold or
    complete them — and settled receipts stay as history; the report derives
    the passenger clawback todo from those bank facts.  This writes no
    decision rows so audit replay stays deterministic.
    """
    db.execute("UPDATE payment_intent SET state='cancelled' WHERE claim_id=? AND state='reserved'",
               (claim_id,))
    db.execute("UPDATE party_debt SET status='void' WHERE claim_id=? AND status='open'", (claim_id,))
