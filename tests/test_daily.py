from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import daily


class DailyTests(unittest.TestCase):
    def test_daily_read_and_page_use_the_same_archived_signal(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'data').mkdir()
            (root/'data'/'last_alert.json').write_text(json.dumps({'month':'2026-08','picks':'USA_VAL+ENERGY'}))
            history={'2026-09':{'factor':'USA_VAL','sector':'INFOTECH','cash':False},
                     '2026-10':{'factor':'USA_MOM','sector':'ENERGY','cash':False}}
            (root/'data'/'signal_history.json').write_text(json.dumps(history))
            with patch.object(daily,'HERE',root),patch.object(daily,'date') as clock:
                clock.today.return_value=date(2026,10,1)
                self.assertEqual(daily.held_now(),('USA_VAL','INFOTECH','2026-09'))


if __name__=='__main__':
    unittest.main()
