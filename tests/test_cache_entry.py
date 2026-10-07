# Import utility modules
import os
import tempfile
import unittest
from unittest import mock

# Import other functions from package
from omics_gateway import env
from omics_gateway.cache_entry import CacheEntry, CacheEntryStatus
from omics_gateway.cache_key import CacheKey
from omics_gateway.gateway import app
from omics_gateway.items.file.fileitem import FileItem
from omics_gateway.items.file.fileitem_source import FileItemSource
from omics_gateway.items.item import ItemType

# Create CacheKey instance using FileItem for .h5ad file and
# FileItemSource pointing to local directory
key = CacheKey(
    FileItem('/czi/', name='pbmc3k.h5ad', type=ItemType.h5ad),
    FileItemSource('/tmp', 'local'),
)


class TestRenderEntry(unittest.TestCase):
    """
    Unit tests for `CacheEntry` rendering and URL rewriting logic.

    Test how static asset paths in HTML/CSS content are rewritten, depending on
    configuration.
    """

    def setUp(self):
        """
        Set up Flask test client and application context for cache entry tests.

        Executed before each test to initialise Flask `test_request_context` and
        client used for simulating requests.
        """

        self.app = app
        self.app_context = self.app.test_request_context()
        self.app_context.push()
        # Popped explicitly: context left on stack leaks into any test module
        # run next, hiding missing contexts there
        self.addCleanup(self.app_context.pop)
        self.client = self.app.test_client()

    def test_GIVEN_key_and_port_THEN_returns_loading_CacheEntry(self):
        """
        Test that creating `CacheEntry` with key and port returns entry with
        status set to `CacheEntryStatus.loading`.
        """

        entry = CacheEntry.for_key('some-key', 1)
        self.assertEqual(entry.status, CacheEntryStatus.loading)

    def test_GIVEN_absolute_static_url_THEN_include_path(self):
        """
        Test that absolute CSS `url()` path is rewritten to include cache entry
        path.
        """

        actual = CacheEntry.for_key(key, 8000).rewrite_text_content(
            'src:url(/static/assets/'
        )
        expected = 'src:url(/view/czi/pbmc3k.h5ad/static/assets/'
        self.assertEqual(actual, expected)

    def test_GIVEN_absolute_src_THEN_include_path(self):
        """
        Test that absolute static `<link>` URL gains cache entry path.
        """

        actual = CacheEntry.for_key(key, 8000).rewrite_text_content(
            '<link rel="shortcut icon" href="/static/assets/favicon.png">'
        )
        expected = '<link rel="shortcut icon" href="/view/czi/pbmc3k.h5ad/static/assets/favicon.png">'
        self.assertEqual(actual, expected)

    def test_GIVEN_body_THEN_export_script_keeps_gateway_static_path(self):
        """
        Test that page body gains image export script under gateway's own
        `/static/`, not rewritten to cellxgene's.
        """

        actual = CacheEntry.for_key(key, 8000).rewrite_text_content(
            '<body></body>'
        )
        self.assertIn(
            '<script src="/static/js/cellxgene_export.js"></script></body>',
            actual,
        )

    def test_GIVEN_body_THEN_defaults_script_keeps_gateway_static_path(self):
        """
        Test that page body gains default colouring and sidebar layout script
        under gateway's own `/static/`.
        """

        actual = CacheEntry.for_key(key, 8000).rewrite_text_content(
            '<body></body>'
        )
        self.assertIn(
            '<script src="/static/js/cellxgene_defaults.js"></script>', actual
        )

    def _defaults_tag(self, tsv_text):
        """
        Render page body with given .tsv content and return defaults script tag.

        Parameters:
        -----------
        tsv_text: str or None
          Content of dataset metadata .tsv file, or None for no file.

        Returns:
        --------
        tag: str
          Opening tag of cellxgene_defaults.js script.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tsv_path = os.path.join(tmp, 'datasets.tsv')
            if tsv_text is not None:
                with open(tsv_path, 'w', newline='') as tsv:
                    tsv.write(tsv_text)
            with mock.patch.object(env, 'dataset_metadata_tsv', tsv_path):
                actual = CacheEntry.for_key(key, 8000).rewrite_text_content(
                    '<body></body>'
                )
        start = actual.index('<script src="/static/js/cellxgene_defaults.js"')
        return actual[start : actual.index('>', start) + 1]

    def test_GIVEN_tsv_default_color_THEN_script_tag_carries_it_escaped(self):
        """
        Test that dataset row's default_color reaches script tag, HTML-escaped.
        """

        tag = self._defaults_tag(
            'file_path\tdefault_color\n'
            'other.h5ad\tSex\n'
            'czi/pbmc3k.h5ad\tcell "type"\n'
        )
        self.assertEqual(
            '<script src="/static/js/cellxgene_defaults.js" '
            'data-default-color="cell &quot;type&quot;">',
            tag,
        )

    def test_GIVEN_no_default_color_THEN_script_tag_has_no_attribute(self):
        """
        Test that empty value, missing column, missing row and missing file
        all leave script's own default.
        """

        plain = '<script src="/static/js/cellxgene_defaults.js">'
        for tsv_text in (
            'file_path\tdefault_color\nczi/pbmc3k.h5ad\t\n',
            'file_path\nczi/pbmc3k.h5ad\n',
            'file_path\tdefault_color\nother.h5ad\tSex\n',
            None,
        ):
            with self.subTest(tsv_text=tsv_text):
                self.assertEqual(plain, self._defaults_tag(tsv_text))


# Entry point for running test suite
if __name__ == '__main__':
    unittest.main()
