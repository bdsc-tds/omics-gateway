import os
import tempfile
import unittest
from unittest import mock

from werkzeug.exceptions import NotFound

from omics_gateway import env, gateway
from omics_gateway.branding import format_stat, load_branding


class TestLoadBranding(unittest.TestCase):
    def setUp(self):
        """
        Create temporary folder holding placeholder favicon for each test.
        """
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with open(os.path.join(self.tmp.name, 'icon.png'), 'wb') as icon:
            icon.write(b'png')

    def write_branding(self, text):
        """
        Write branding YAML into temporary folder and return its path.
        """
        path = os.path.join(self.tmp.name, 'branding.yaml')
        with open(path, 'w') as branding_file:
            branding_file.write(text)
        return path

    def test_default_branding_is_valid(self):
        """
        Test that branding shipped with repository loads, with every optional
        key unset and favicon resolved to existing file.
        """
        branding = load_branding(env.default_branding_file)
        self.assertEqual('Omics Gateway', branding['window_title'])
        self.assertTrue(os.path.isfile(branding['favicon_path']))
        self.assertIsNone(branding['logo_path'])
        self.assertIsNone(branding['contact_email'])

    def test_paths_resolve_against_branding_folder(self):
        """
        Test that relative favicon path is made absolute from folder holding
        branding file, not from working directory.
        """
        path = self.write_branding(
            'window_title: W\npage_title: P\nfavicon_path: icon.png\n'
            'homepage_title: T\nhomepage_subtitle: S\n'
        )
        branding = load_branding(path)
        self.assertEqual(
            os.path.join(self.tmp.name, 'icon.png'), branding['favicon_path']
        )

    def test_all_problems_reported_together(self):
        """
        Test that one error names every missing, unknown and file-less key.
        """
        path = self.write_branding(
            'window_title: W\npage_titel: typo\nfavicon_path: absent.png\n'
            'homepage_title: T\nhomepage_subtitle: S\n'
        )
        with self.assertRaises(ValueError) as raised:
            load_branding(path)
        message = str(raised.exception)
        self.assertIn('page_title: missing', message)
        self.assertIn('page_titel: unknown key', message)
        self.assertIn('favicon_path: file not found', message)

    def test_numbers_become_text_and_lists_are_rejected(self):
        """
        Test that bare YAML number is kept as text, while list value is
        reported as not text.
        """
        base = (
            'window_title: W\npage_title: P\nfavicon_path: icon.png\n'
            'homepage_title: T\n'
        )
        path = self.write_branding(
            base + 'homepage_subtitle: S\ncell_types_stat: 30\n'
        )
        self.assertEqual('30', load_branding(path)['cell_types_stat'])

        path = self.write_branding(base + 'homepage_subtitle: [a, b]\n')
        with self.assertRaises(ValueError) as raised:
            load_branding(path)
        self.assertIn('homepage_subtitle: must be text', str(raised.exception))

    def test_non_mapping_file_is_rejected(self):
        """
        Test that YAML list instead of key-value map raises TypeError.
        """
        path = self.write_branding('- window_title\n')
        with self.assertRaises(TypeError):
            load_branding(path)

    def test_missing_file_is_reported(self):
        """
        Test that absent branding file raises ValueError naming its path.
        """
        path = os.path.join(self.tmp.name, 'absent.yaml')
        with self.assertRaises(ValueError) as raised:
            load_branding(path)
        self.assertIn(path, str(raised.exception))


class TestFormatStat(unittest.TestCase):
    def test_rounds_down_to_two_significant_figures(self):
        """
        Test exact small counts and rounded K and M labels.
        """
        self.assertEqual('11', format_stat(11))
        self.assertEqual('999', format_stat(999))
        self.assertEqual('1.5K+', format_stat(1_599))
        self.assertEqual('12K+', format_stat(12_345))
        self.assertEqual('760K+', format_stat(760_930))
        self.assertEqual('1.2M+', format_stat(1_234_567))


class TestBrandingRoutes(unittest.TestCase):
    def test_unset_logo_gives_404(self):
        """
        Test that logo route answers 404 when branding sets no logo, and
        unknown asset name does too.
        """
        branding = dict(gateway.branding, logo_path=None)
        with (
            mock.patch.object(gateway, 'branding', branding),
            gateway.app.test_request_context('/branding/logo'),
        ):
            with self.assertRaises(NotFound):
                gateway.branding_asset('logo')
            with self.assertRaises(NotFound):
                gateway.branding_asset('secrets')

    def test_homepage_counts_come_from_dataset_table(self):
        """
        Test that homepage sums cells over every dataset row, merged ones
        included, and shows cell types only when branding sets them.
        """
        rows = [{'cell_count': '500000'}, {'cell_count': '260930'}, {}]
        with (
            tempfile.NamedTemporaryFile(suffix='.tsv') as tsv,
            mock.patch.object(env, 'dataset_metadata_tsv', tsv.name),
            mock.patch.object(
                gateway,
                'load_dataset_metadata_tsv',
                return_value=(rows,) + (None,) * 7,
            ),
            mock.patch.object(
                gateway,
                'branding',
                dict(gateway.branding, cell_types_stat='30+'),
            ),
            gateway.app.test_request_context('/'),
        ):
            html = gateway.homepage()
        self.assertIn('760K+', html)
        self.assertIn('>3</div>', html)
        self.assertIn('30+', html)

    def test_homepage_hides_counts_without_dataset_table(self):
        """
        Test that dataset and cell cards disappear when .tsv is absent.
        """
        with (
            mock.patch.object(env, 'dataset_metadata_tsv', '/absent.tsv'),
            gateway.app.test_request_context('/'),
        ):
            html = gateway.homepage()
        self.assertNotIn('stat-card', html)


if __name__ == '__main__':
    unittest.main()
