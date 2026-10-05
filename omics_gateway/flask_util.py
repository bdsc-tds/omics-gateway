# Copyright 2019 Novartis Institutes for BioMedical Research Inc. Licensed
# under the Apache License, Version 2.0 (the "License"); you may not use
# this file except in compliance with the License. You may obtain a copy
# of the License at http://www.apache.org/licenses/LICENSE-2.0. Unless
# required by applicable law or agreed to in writing, software distributed
# under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES
# OR CONDITIONS OF ANY KIND, either express or implied. See the License for
# the specific language governing permissions and limitations under the License.
# Modified by CHUV, 2025-2026.


# Import utility modules
from flask import request, url_for


def querystring():
    """
    Get current request query string, prefixed with question mark if not empty.

    Returns:
    --------
    str
      Decoded query string with "?" prefix, or empty string if none.
    """
    qs = request.query_string.decode()
    return f'?{qs}' if len(qs) > 0 else ''


def view_url(descriptor):
    """
    Generate URL to view dataset

    Parameters:
    -----------
    descriptor: str
      Path or identifier for dataset.

    Returns:
    --------
    str
      URL string pointing to dataset view endpoint.
    """
    return url_for('do_view', path=descriptor)


def relaunch_url(descriptor):
    """
    Generate URL to relaunch dataset.

    Parameters:
    -----------
    descriptor: str
      Path or identifier for dataset.

    Returns:
    --------
    str
      URL string pointing to dataset relaunch endpoint.
    """
    return url_for('do_relaunch', path=descriptor)
