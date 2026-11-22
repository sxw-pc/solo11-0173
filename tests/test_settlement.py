import json
from pathlib import Path
from tempfile import TemporaryDirectory

from bagchain.source import import_file
from bagchain.settlement import pay
from bagchain.report import report


FIXTURES = Path(__file__).resolve().parent.parent / 'fixtures'


def test_basic_late_payment():
    with TemporaryDirectory() as directory:
        root = Path(directory)
        db, bank = root / 'case.sqlite', root / 'bank.json'
        import_file(db, 'bag', FIXTURES / 'bag-ok.json')
        sort = json.loads((FIXTURES / 'sort-ok.json').read_text())
        sort['events'] = sort['events'][:3]
        partial = root / 'partial.json'
        partial.write_text(json.dumps(sort))
        import_file(db, 'sort', partial)
        assert import_file(db, 'claim', FIXTURES / 'late-claim.json')['results'][0]['status'] == 'trace'
        import_file(db, 'sort', FIXTURES / 'late-arrival.json')
        assert report(db, 'BAG-1')['claims'][0]['status'] == 'compensable'
        assert pay(db, bank, FIXTURES / 'payment-ok.json')['status'] == 'paid'
        assert report(db, 'BAG-1')['settlements'][0]['amount_cents'] == 20000
        assert json.loads(bank.read_text())[0]['status'] == 'transferred'
