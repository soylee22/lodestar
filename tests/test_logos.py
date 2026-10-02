import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from pipeline import logos


class LogoTests(unittest.TestCase):
    def test_unavailable_images_leave_a_usable_empty_registry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'data').mkdir()
            registry = root / 'data/company_logos.json'
            with patch.object(logos, 'HERE', root), patch.object(logos, 'ASSETS', root / 'assets/company-logos'), patch.object(logos, 'REGISTRY', registry), patch('pipeline.logos.urlopen', side_effect=OSError('unavailable')):
                result = logos.refresh([{'symbol': 'MU', 'name': 'Micron'}])
            self.assertEqual(result, {})
            self.assertEqual(json.loads(registry.read_text()), {})
            registry.write_text('{broken')
            self.assertEqual(logos.load_registry(registry), {})


if __name__ == '__main__':
    unittest.main()
