# Import utility modules
import os
import tempfile
import unittest

# Import other functions from package
from omics_gateway.items.file.fileitem_source import FileItemSource


class TestFileItemSource(unittest.TestCase):
    """
    Unit tests for `FileItemSource` class.

    Verify file and directory behavior for building item trees and creating
    `FileItem` instances based on local filesystem paths.
    """

    def test_list_items_GIVEN_real_folder_THEN_returns_h5ad_tree(self):
        """
        Test that `list_items` builds tree of `.h5ad` files from real folder,
        dropping other files and folders without data, scoping scan to filter,
        and raising for missing folder.
        """

        with tempfile.TemporaryDirectory() as base:
            for path in ('a.h5ad', 'notes.txt', 'study/b.h5ad'):
                full_path = os.path.join(base, path)
                os.makedirs(os.path.dirname(full_path), exist_ok=True)
                open(full_path, 'w').close()
            os.mkdir(os.path.join(base, 'empty'))
            source = FileItemSource(base, 'local')

            tree = source.list_items()
            self.assertEqual(['a.h5ad'], [item.name for item in tree.items])
            self.assertEqual(
                ['study'], [branch.descriptor for branch in tree.branches]
            )
            self.assertEqual(
                ['study/b.h5ad'],
                [item.descriptor for item in tree.branches[0].items],
            )

            filtered = source.list_items('study')
            self.assertEqual(
                ['study/b.h5ad'], [item.descriptor for item in filtered.items]
            )

            with self.assertRaises(FileNotFoundError):
                source.list_items('missing')

    def test_make_fileitem_from_path_GIVEN_annotation_file_THEN_name_lacks_csv(
        self,
    ):
        """
        Test that `make_fileitem_from_path` correctly strips `.csv` from
        annotation filenames.
        """

        source = FileItemSource(tempfile.gettempdir(), 'local')
        item = source.make_fileitem_from_path(
            'customanno.csv', 'someh5ad_annotations', True
        )
        self.assertEqual(item.name, 'customanno')
        self.assertEqual(item.descriptor, 'someh5ad_annotations/customanno.csv')

    def test_make_fileitem_from_path_GIVEN_h5ad_file_THEN_returns_name(self):
        """
        Test that `make_fileitem_from_path` for `.h5ad` files returns full
        filename.
        """

        source = FileItemSource(tempfile.gettempdir(), 'local')
        item = source.make_fileitem_from_path('someanalysis.h5ad', 'studydir')
        self.assertEqual(item.name, 'someanalysis.h5ad')
        self.assertEqual(item.descriptor, 'studydir/someanalysis.h5ad')
