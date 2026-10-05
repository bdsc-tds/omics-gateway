# Import utility modules
import importlib
import os
import tempfile
import unittest
from unittest import mock

# Import other functions from package
from werkzeug.exceptions import HTTPException

from omics_gateway import dataset_metadata_loader as dml
from omics_gateway import env, gateway
from omics_gateway.cache_entry import CacheEntryStatus
from omics_gateway.cache_exception import CacheException


# Function to build os.environ replacement with keys removed and added
def _environ_without(unset, **overrides):
    """
    Build copy of os.environ with some keys removed and others set.

    Parameters:
    -----------
    unset: iterable of str
      Variable names to remove entirely, so code under test sees them as unset
      rather than empty.
    overrides: str
      Variable names and values to set.

    Returns:
    --------
    patched: dict
      Environment mapping suitable for mock.patch.dict(..., clear=True).
    """
    patched = {k: v for k, v in os.environ.items() if k not in unset}
    patched.update(overrides)

    return patched


class EnvReloadCase(unittest.TestCase):
    """
    Base case for tests that need env.py re-evaluated under different
    environment. env.py reads os.environ at import time, so changing variable
    afterwards has no effect until module is reloaded.
    """

    def reload_env(self, unset=(), **overrides):
        """
        Reload env module with patched environment, restoring both on test
        teardown.

        Parameters:
        -----------
        unset: iterable of str
          Variable names that reloaded module should see as unset.
        overrides: str
          Variable names and values that reloaded module should see.

        Returns:
        --------
        None
        """
        patcher = mock.patch.dict(
            os.environ, _environ_without(unset, **overrides), clear=True
        )
        patcher.start()
        self.addCleanup(importlib.reload, env)
        self.addCleanup(patcher.stop)
        importlib.reload(env)


class TestQcDataResolution(EnvReloadCase):
    """
    Pin behaviour of env.qc_data, which is already centralised and absolutised.
    This model should be followed by other data paths.
    """

    def test_GIVEN_qc_data_unset_THEN_default_is_absolute(self):
        """
        Test that analysis_qc default is absolutised, since Flask resolves
        relative directory against package dir rather than cwd.
        """
        self.reload_env(unset=['QC_DATA'])
        self.assertTrue(os.path.isabs(env.qc_data))
        self.assertTrue(env.qc_data.endswith('analysis_qc'))

    def test_GIVEN_qc_data_relative_THEN_absolutised(self):
        """
        Test that relative QC_DATA is expanded against working directory.
        """
        self.reload_env(QC_DATA='some_qc_dir')
        self.assertEqual(os.path.abspath('some_qc_dir'), env.qc_data)

    def test_GIVEN_qc_data_absolute_THEN_unchanged(self):
        """
        Test that absolute QC_DATA is passed through as given.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.reload_env(QC_DATA=tmp)
            self.assertEqual(tmp, env.qc_data)


class TestCellxgeneDataResolution(EnvReloadCase):
    """
    Check GATEWAY_DATA is resolved in one place (env.py) and that every
    consumer agrees with it whether or not variable is set.
    """

    def _effective_loader_data_dir(self):
        """
        Capture data directory used by metadata loader when its caller passes
        none.

        Returns:
        --------
        data_dir: str
          Value forwarded by loader to find_annotations_for_file.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tsv_path = os.path.join(tmp, 'datasets.tsv')
            with open(tsv_path, 'w', newline='') as tsv:
                tsv.write('dataset_id\tfile_path\nx\tx.h5ad\n')
            with mock.patch.object(
                dml, 'find_annotations_for_file', return_value=([], [])
            ) as find:
                dml.load_dataset_metadata_tsv(tsv_path, data_dir=None)

            return find.call_args[0][1]

    def _effective_gateway_base_path(self):
        """
        Capture base path handed by gateway to its local item source.

        Returns:
        --------
        base_path: str
          Value passed as FileItemSource's base_path.
        """
        # initialise_data_sources sets module-level source; restore it or
        # unrelated tests inherit source set up here
        self.addCleanup(setattr, gateway, 'item_source', gateway.item_source)
        # Patched at its source module: gateway imports it inside function
        with mock.patch(
            'omics_gateway.items.file.fileitem_source.FileItemSource'
        ) as source:
            gateway.initialise_data_sources()

        return source.call_args[0][0]

    def test_GIVEN_unset_THEN_loader_takes_fallback_from_env(self):
        """
        Test that metadata loader has no default of its own and reads resolved
        value from env module instead.
        """
        self.reload_env(unset=['GATEWAY_DATA'])
        with mock.patch.object(env, 'gateway_data', '/sentinel/data'):
            self.assertEqual(
                '/sentinel/data', self._effective_loader_data_dir()
            )

    def test_GIVEN_relative_path_THEN_env_module_absolutises_it(self):
        """
        Test that relative directory is made absolute, since Flask resolves
        relative directories against package directory rather than working one.
        """
        self.reload_env(GATEWAY_DATA='relative_data')
        self.assertEqual(os.path.abspath('relative_data'), env.gateway_data)

    def test_GIVEN_unset_THEN_gateway_raises(self):
        """
        Test that gateway refuses to start without data directory. Its None
        default is what makes this check reachable, so any unification must
        keep 'unset' distinguishable from 'set'.
        """
        self.reload_env(unset=['GATEWAY_DATA'])
        with self.assertRaises(ValueError) as raised:
            self._effective_gateway_base_path()
        self.assertIn('GATEWAY_DATA', str(raised.exception))

    def test_GIVEN_set_THEN_all_three_consumers_agree(self):
        """
        Test that env, metadata loader and gateway resolve same directory when
        GATEWAY_DATA is set.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self.reload_env(GATEWAY_DATA=tmp)
            self.assertEqual(tmp, env.gateway_data)
            self.assertEqual(tmp, self._effective_loader_data_dir())
            self.assertEqual(tmp, self._effective_gateway_base_path())

    def test_GIVEN_unset_THEN_all_three_consumers_agree(self):
        """
        Test that no consumer invents data directory when variable is unset.
        Loader coerces None to '' because its path joins would otherwise raise
        TypeError into exception handler that hides it.
        """
        self.reload_env(unset=['GATEWAY_DATA'])
        self.assertIsNone(env.gateway_data)
        self.assertEqual('', self._effective_loader_data_dir())


class TestSpatialMetricsFlag(EnvReloadCase):
    """
    Check SPATIAL_METRICS picks which generated config spatial rows link to.
    """

    def _spatial_config(self):
        """
        Load one-row catalogue holding spatial dataset and return its config.

        Returns:
        --------
        spatial_config: str
          Config URL metadata loader attached to row.
        """
        with tempfile.TemporaryDirectory() as tmp:
            tsv_path = os.path.join(tmp, 'datasets.tsv')
            with open(tsv_path, 'w', newline='') as tsv:
                tsv.write('dataset_id\tfile_path\nx\tsample.zarr\n')
            datasets = dml.load_dataset_metadata_tsv(tsv_path, data_dir=tmp)[0]

        return datasets[0]['spatial_config']

    def test_GIVEN_unset_THEN_config_with_metrics_linked(self):
        """
        Test that Metric layer is on unless disabled.
        """
        self.reload_env(unset=['SPATIAL_METRICS'])
        self.assertEqual(
            '/spatial-data/vitessce_configs/sample.vitessce.json',
            self._spatial_config(),
        )

    def test_GIVEN_false_THEN_config_without_metrics_linked(self):
        """
        Test that disabling metrics links config generator writes without them.
        """
        self.reload_env(SPATIAL_METRICS='false')
        self.assertEqual(
            '/spatial-data/vitessce_configs/sample.nometrics.vitessce.json',
            self._spatial_config(),
        )


class TestDataPathRoutes(EnvReloadCase):
    """
    Guard routes that serve files off disk. These previously 404'd because
    Flask resolves relative directory against package directory rather than
    working directory, and nothing covered them.
    """

    def setUp(self):
        """
        Create data directory (dataset, spatial store and its config), QC
        directory with one figure, and files outside data directory that no
        route may serve, then point environment at both directories.

        Returns:
        --------
        None
        """
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = os.path.join(self.tmp.name, 'data')
        self.qc_dir = os.path.join(self.tmp.name, 'analysis_qc', 'ds1')
        os.makedirs(self.data_dir)
        os.makedirs(self.qc_dir)
        with open(os.path.join(self.data_dir, 'sample.h5ad'), 'wb') as h5ad:
            h5ad.write(b'not really an h5ad')
        with open(os.path.join(self.qc_dir, 'plot.png'), 'wb') as png:
            png.write(b'not really a png')
        for path in (
            'data/store.zarr/zarr.json',
            'data/vitessce_configs/store.vitessce.json',
            'secret.txt',
            'secret.h5ad',
        ):
            full_path = os.path.join(self.tmp.name, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as file:
                file.write(b'0123456789')
        self.reload_env(
            GATEWAY_DATA=self.data_dir,
            QC_DATA=os.path.join(self.tmp.name, 'analysis_qc'),
        )

    def test_GIVEN_existing_file_THEN_download_serves_it(self):
        """
        Test that /download returns file rather than 404.
        """
        with gateway.app.test_request_context('/download/sample.h5ad'):
            response = gateway.download_file('sample.h5ad')
        self.addCleanup(response.close)
        self.assertEqual(200, response.status_code)

    def test_GIVEN_existing_image_THEN_qc_image_serves_it(self):
        """
        Test that /qc-image returns figure rather than 404.
        """
        with gateway.app.test_request_context('/qc-image/ds1/plot.png'):
            response = gateway.qc_image('ds1', 'plot.png')
        self.addCleanup(response.close)
        self.assertEqual(200, response.status_code)

    def test_GIVEN_traversal_THEN_qc_image_rejected(self):
        """
        Test that path traversal attempt is refused rather than served.
        """
        with (
            gateway.app.test_request_context('/qc-image/ds1/x'),
            self.assertRaises(Exception) as raised,
        ):
            gateway.qc_image('ds1', '../../etc/passwd')
        self.assertIn('400', str(getattr(raised.exception, 'http_status', '')))

    def request_status(self, view, *args, url='/', headers=None):
        """
        Call route function inside request context and return HTTP status it
        produces, whether returned as response or raised as error.

        Parameters:
        -----------
        view: callable
          Route function to call.
        args: str
          Positional arguments for route function.
        url: str
          Request URL, query string included.
        headers: dict or None
          Request headers.

        Returns:
        --------
        status: int
          HTTP status code of response or raised error.
        """
        with gateway.app.test_request_context(url, headers=headers):
            try:
                response = gateway.app.make_response(view(*args))
            except CacheException as error:
                return error.http_status
            except HTTPException as error:
                return error.code
        response.close()
        return response.status_code

    def test_GIVEN_escaping_dataset_id_THEN_qc_routes_reject_it(self):
        """
        Test that /qc and /qc-image refuse dataset id pointing outside QC
        directory, including sibling folder sharing its name as prefix.
        """
        for dataset_id in ('..', '../analysis_qc_thumbs'):
            with self.subTest(dataset_id=dataset_id):
                self.assertEqual(
                    400, self.request_status(gateway.qc_report, dataset_id)
                )
                self.assertEqual(
                    400,
                    self.request_status(
                        gateway.qc_image, dataset_id, 'secret.txt'
                    ),
                )

    def test_GIVEN_unsafe_name_THEN_download_rejected(self):
        """
        Test that /download refuses traversal, subfolders and non-.h5ad files,
        including files that exist outside data directory.
        """
        for filename in (
            '../secret.h5ad',
            'sub/sample.h5ad',
            '..\\secret.h5ad',
            'secret.txt',
            'sample.h5ad.txt',
        ):
            with self.subTest(filename=filename):
                self.assertEqual(
                    400, self.request_status(gateway.download_file, filename)
                )

    def test_GIVEN_missing_file_THEN_download_returns_404(self):
        """
        Test that /download reports missing dataset as 404.
        """
        self.assertEqual(
            404, self.request_status(gateway.download_file, 'absent.h5ad')
        )

    def test_GIVEN_existing_file_THEN_spatial_data_serves_byte_ranges(self):
        """
        Test that /spatial-data serves files from data directory, and answers
        range requests with 206, which Vitessce needs to stream chunks.
        """
        path = 'store.zarr/zarr.json'
        self.assertEqual(200, self.request_status(gateway.spatial_data, path))
        self.assertEqual(
            206,
            self.request_status(
                gateway.spatial_data, path, headers={'Range': 'bytes=0-3'}
            ),
        )

    def test_GIVEN_unsafe_path_THEN_spatial_data_refuses_it(self):
        """
        Test that /spatial-data never serves file outside data directory, by
        traversal or absolute path, and reports missing files as 404.
        """
        for subpath, status in (
            ('../secret.txt', 400),
            ('store.zarr/../../secret.txt', 400),
            (os.path.join(self.tmp.name, 'secret.txt'), 404),
            ('store.zarr/absent', 404),
        ):
            with self.subTest(subpath=subpath):
                self.assertEqual(
                    status, self.request_status(gateway.spatial_data, subpath)
                )

    def test_GIVEN_config_THEN_spatial_viewer_checks_it(self):
        """
        Test that /spatial-viewer opens config hosted in data directory, and
        refuses missing, foreign, escaping or absent configs.
        """
        prefix = '/spatial-viewer?config='
        for config, status in (
            ('/spatial-data/vitessce_configs/store.vitessce.json', 200),
            ('', 400),
            ('https://example.org/store.vitessce.json', 400),
            ('/static/store.vitessce.json', 400),
            ('/spatial-data/../secret.txt', 404),
            ('/spatial-data/vitessce_configs/absent.vitessce.json', 404),
        ):
            with self.subTest(config=config):
                self.assertEqual(
                    status,
                    self.request_status(
                        gateway.spatial_viewer, url=prefix + config
                    ),
                )

    def test_GIVEN_cellxgene_state_THEN_view_static_proxies_to_loaded(self):
        """
        Test that /view/static returns 503 without loaded cellxgene process,
        and otherwise forwards path to loaded process only.
        """
        self.addCleanup(
            setattr, gateway.cache, 'entry_list', gateway.cache.entry_list
        )
        loading = mock.Mock(status=CacheEntryStatus.loading, port=8001)
        loaded = mock.Mock(status=CacheEntryStatus.loaded, port=8002)

        gateway.cache.entry_list = [loading]
        self.assertEqual(
            503, self.request_status(gateway.view_static, 'logo.png')
        )

        gateway.cache.entry_list = [loading, loaded]
        upstream = mock.Mock(
            content=b'png', status_code=200, headers={'Content-Type': 'x'}
        )
        with mock.patch('requests.get', return_value=upstream) as get:
            status = self.request_status(gateway.view_static, 'logo.png')
        self.assertEqual(200, status)
        get.assert_called_once_with('http://127.0.0.1:8002/static/logo.png')


if __name__ == '__main__':
    unittest.main()
