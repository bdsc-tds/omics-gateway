# Import utility modules
import os

# Import other functions from package
from omics_gateway import env

# QC folders are split by data type, like data directories
QC_TYPES = ('scrna', 'spatial')


def find_qc_dir(dataset_id):
    """
    Function to locate dataset's QC folder under any data type folder of QC
    root. Dataset ids are unique across types, so first match wins.

    Parameters:
    -----------
    dataset_id: str
      Dataset identifier, as in dataset table.

    Returns:
    --------
    qc_dir: str or None
      Absolute path of dataset QC folder, or None if no type folder has one.

    Raises:
    -------
    ValueError
      If dataset_id points outside its type folder or at type folder itself.
    """
    for data_type in QC_TYPES:
        type_dir = os.path.normpath(os.path.join(env.qc_data, data_type))
        qc_dir = os.path.normpath(os.path.join(type_dir, dataset_id))
        escapes = os.path.commonpath([type_dir, qc_dir]) != type_dir
        # Type folder itself ('', '.') is not dataset either
        if escapes or qc_dir == type_dir:
            raise ValueError(f'Invalid dataset id: {dataset_id!r}')
        if os.path.isdir(qc_dir):
            return qc_dir
    return None
