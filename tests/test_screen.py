import tempfile
import unittest
from pathlib import Path
from bagchain.source import import_file
from bagchain.report import report
from bagchain.audit import audit

ROOT=Path(__file__).resolve().parent.parent

class ScreeningSeedTests(unittest.TestCase):
    def test_raw_scan_and_provisional_single_material_label(self):
        with tempfile.TemporaryDirectory() as temp:
            db=Path(temp)/'bag.sqlite'
            import_file(db,'bag',ROOT/'fixtures/bag-ok.json')
            import_file(db,'screen',ROOT/'fixtures/screen-ok.json')
            result=report(db,'BAG-1')
            self.assertEqual(result['screen_scans'][0]['state'],'captured')
            self.assertEqual((result['screen_labels'][0]['material'],result['screen_labels'][0]['distance']),('water',1))
            self.assertEqual(audit(db)['status'],'ok')

if __name__=='__main__':unittest.main()
