import codecs
import os

from setuptools import find_packages, setup


def read(rel_path):
    here = os.path.abspath(os.path.dirname(__file__))
    with codecs.open(os.path.join(here, rel_path), 'r') as fp:
        return fp.read()


def get_version(rel_path):
    for line in read(rel_path).splitlines():
        if line.startswith('__version__'):
            delim = '"' if '"' in line else "'"
            return line.split(delim)[1]
    raise RuntimeError('Unable to find version string.')


with open('README.md', 'r') as fh:
    long_description = fh.read()

setup(
    # mandatory
    name='omics-gateway',
    # mandatory
    version=get_version('omics_gateway/__init__.py'),
    # mandatory
    author='CHUV',
    description=('Web portal for browsing and exploring omics datasets'),
    long_description=long_description,
    long_description_content_type='text/markdown',
    license='Apache-2.0',
    keywords='visualization, genomics',
    url='https://github.com/bdsc-tds/omics-gateway',
    packages=find_packages(),
    package_data={
        'omics_gateway': [
            'static/css/*.css',
            'templates/*.html',
            'static/js/*.js',
            'static/cell.jpg',
        ]
    },
    data_files=[('', ['README.md', 'LICENSE'])],
    entry_points={
        'console_scripts': ['omics-gateway=omics_gateway.gateway:main']
    },
    classifiers=['Topic :: Scientific/Engineering :: Visualization'],
    python_requires='>=3.6',
)
