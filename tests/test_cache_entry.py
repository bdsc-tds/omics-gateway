# Import utility modules
import unittest

# Import other functions from package
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


# Entry point for running test suite
if __name__ == '__main__':
    unittest.main()
