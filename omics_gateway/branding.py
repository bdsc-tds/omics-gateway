# Import utility modules
import os

import yaml

# Keys branding file must set; start-up fails naming each one missing
MANDATORY_KEYS = (
    'window_title',
    'page_title',
    'favicon_path',
    'homepage_title',
    'homepage_subtitle',
)
OPTIONAL_KEYS = (
    'logo_path',
    'logo_url',
    'logo_alt',
    'contact_email',
    'cell_types_stat',
)
# Resolved against folder holding branding file; must point to existing file
PATH_KEYS = ('favicon_path', 'logo_path')
# Served under /branding/<asset>, mapped to their path key
ASSET_KEYS = {'favicon': 'favicon_path', 'logo': 'logo_path'}


def load_branding(branding_path):
    """
    Function to read and check deployment branding file.

    Every problem is collected before raising, so one start-up attempt reports
    all of them. Unknown keys count as problems, catching typos that would
    otherwise fall back to nothing silently.

    Parameters:
    -----------
    branding_path: str
      Path to branding YAML file.

    Returns:
    --------
    branding: dict
      Every known key, None when unset, with path keys made absolute.

    Raises:
    -------
    ValueError
      If file is unreadable, or any key is missing, unknown, not text, or
      names absent file.
    TypeError
      If file content is not key-value map.
    """
    try:
        with open(branding_path) as branding_file:
            content = yaml.safe_load(branding_file)
    except (OSError, yaml.YAMLError) as error:
        raise ValueError(f'Cannot read branding file {branding_path}: {error}')
    if content is None:
        content = {}
    if not isinstance(content, dict):
        raise TypeError(f'Branding file {branding_path} is not key-value map')

    problems = []
    for key in MANDATORY_KEYS:
        if content.get(key) in (None, ''):
            problems.append(f'{key}: missing')
    for key in sorted(set(content) - set(MANDATORY_KEYS + OPTIONAL_KEYS)):
        problems.append(f'{key}: unknown key')

    branding_dir = os.path.dirname(os.path.abspath(branding_path))
    branding = {key: None for key in MANDATORY_KEYS + OPTIONAL_KEYS}
    for key in MANDATORY_KEYS + OPTIONAL_KEYS:
        value = content.get(key)
        if value in (None, ''):
            continue
        # YAML reads bare 30 as int; text keeps templates and paths uniform
        if not isinstance(value, (str, int, float)) or isinstance(value, bool):
            problems.append(f'{key}: must be text')
            continue
        value = str(value)
        if key in PATH_KEYS:
            value = os.path.join(branding_dir, value)
            if not os.path.isfile(value):
                problems.append(f'{key}: file not found ({value})')
        branding[key] = value

    if problems:
        raise ValueError(
            f'Invalid branding file {branding_path}:\n  '
            + '\n  '.join(problems)
        )
    return branding


def format_stat(count):
    """
    Function to round homepage statistic down to two significant figures.

    Parameters:
    -----------
    count: int
      Exact count, for example total cells.

    Returns:
    --------
    label: str
      Count below 1000 unchanged, otherwise rounded down with K or M suffix
      and trailing '+', for example '760K+' or '1.2M+'.
    """
    if count < 1000:
        return str(count)
    magnitude = 10 ** (len(str(count)) - 2)
    rounded = count // magnitude * magnitude
    if rounded >= 1_000_000:
        value, suffix = rounded / 1_000_000, 'M'
    else:
        value, suffix = rounded / 1000, 'K'
    return f'{value:g}{suffix}+'
