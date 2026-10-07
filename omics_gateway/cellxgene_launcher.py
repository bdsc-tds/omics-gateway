"""
Script to start cellxgene with missing category values shown as 'Unknown'.

Wraps cellxgene's loader, so each cellxgene process fills them once, in
memory; .h5ad files are untouched. Run by subprocess_backend in isolated mode.
"""

import sys

from server.cli.cli import cli
from server.data_anndata.anndata_adaptor import AnndataAdaptor

load_data = AnndataAdaptor._load_data


def load_and_label(self, data_locator):
    """Load dataset as cellxgene does, then label missing category values."""
    load_data(self, data_locator)
    for name, column in self.data.obs.select_dtypes('category').items():
        if column.isna().any():
            if 'Unknown' not in column.cat.categories:
                column = column.cat.add_categories('Unknown')
            self.data.obs[name] = column.fillna('Unknown')


if __name__ == '__main__':
    AnndataAdaptor._load_data = load_and_label
    sys.argv[0] = 'cellxgene'
    sys.exit(cli())
