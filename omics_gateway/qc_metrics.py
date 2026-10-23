# Import utility modules
import json
import os

# Formats metrics.json columns may name
METRIC_FORMATS = ('integer', 'millions', 'percent', 'decimal')


def format_metric(value, column):
    """
    Function to render one metric value as table cell text.

    Parameters:
    -----------
    value: int, float or None
      Raw metric value; None when sample lacks it.
    column: dict
      Column spec from metrics.json: 'format', optional 'digits' (decimals for
      'millions', 'percent' and 'decimal', default 1) and optional 'unit'.

    Returns:
    --------
    text: str
      Formatted value, or dash for missing value.
    """
    if value is None:
        return '–'
    fmt = column['format']
    digits = column.get('digits', 1)
    if fmt == 'integer':
        text = f'{value:,.0f}'
    elif fmt == 'millions':
        text = f'{value / 1e6:.{digits}f} M'
    elif fmt == 'percent':
        text = f'{100 * value:.{digits}f} %'
    else:
        text = f'{value:.{digits}f}'
    unit = column.get('unit')
    return f'{text} {unit}' if unit else text


def _check(condition, message):
    """
    Function to reject malformed metrics.json with readable reason.

    Parameters:
    -----------
    condition: bool
      Whether spec part is valid.
    message: str
      Reason shown on QC page when it is not.

    Returns:
    --------
    None

    Raises:
    -------
    ValueError
      If condition is false.
    """
    if not condition:
        raise ValueError(message)


def _shape_table(spec, qc_dir):
    """
    Function to validate metrics.json content and format its cells.

    Parameters:
    -----------
    spec: object
      Parsed metrics.json.
    qc_dir: str
      Absolute path of dataset QC folder, against which report paths resolve.

    Returns:
    --------
    table: dict
      'title', 'note', 'groups' (as in spec), 'rows' (each 'sample',
      'description', 'cells' of formatted 'text' and 'group_start' flag for
      first column of group, and 'report' path or None) and 'has_reports'.

    Raises:
    -------
    ValueError
      If spec is malformed or names report absent from QC folder.
    """
    _check(isinstance(spec, dict), 'top level must be an object')
    groups = spec.get('groups')
    _check(isinstance(groups, list) and groups, "'groups' must be a list")
    columns = []
    # Indices of first column of each group, preceded by spacer on page
    group_starts = set()
    for group in groups:
        _check(
            isinstance(group, dict)
            and isinstance(group.get('columns'), list)
            and group['columns'],
            'each group needs a list of columns',
        )
        group_starts.add(len(columns))
        for column in group['columns']:
            _check(
                isinstance(column, dict)
                and isinstance(column.get('key'), str)
                and isinstance(column.get('label'), str),
                'each column needs a key and a label',
            )
            _check(
                column.get('format') in METRIC_FORMATS,
                f"column '{column['key']}' has unknown format "
                f'{column.get("format")!r}',
            )
            columns.append(column)

    rows = []
    _check(isinstance(spec.get('rows'), list), "'rows' must be a list")
    for row in spec['rows']:
        _check(
            isinstance(row, dict) and isinstance(row.get('sample'), str),
            'each row needs a sample name',
        )
        values = row.get('values', {})
        _check(isinstance(values, dict), f'{row["sample"]}: bad values')
        cells = []
        for index, column in enumerate(columns):
            value = values.get(column['key'])
            # bool is int subclass, but never metric
            _check(
                value is None
                or (
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                ),
                f"{row['sample']}: '{column['key']}' is not a number",
            )
            cells.append({
                'text': format_metric(value, column),
                'group_start': index in group_starts,
            })
        report = row.get('report')
        if report is not None:
            _check(
                isinstance(report, str)
                and not os.path.isabs(report)
                and '..' not in report
                and os.path.isfile(os.path.join(qc_dir, report)),
                f'{row["sample"]}: report {report!r} not found',
            )
        rows.append({
            'sample': row['sample'],
            'description': row.get('description'),
            'cells': cells,
            'report': report,
        })

    return {
        'title': spec.get('title'),
        'note': spec.get('note'),
        'groups': groups,
        'rows': rows,
        'has_reports': any(row['report'] for row in rows),
    }


def load_metrics_table(qc_dir):
    """
    Function to read dataset's metrics.json, if any, and shape it for QC page.
    Malformed file yields error message instead, so rest of page still shows.

    Parameters:
    -----------
    qc_dir: str
      Absolute path of dataset QC folder.

    Returns:
    --------
    table: dict or None
      None without metrics.json; else table as returned by _shape_table, or
      {'error': message} when file cannot be read or is malformed.
    """
    path = os.path.join(qc_dir, 'metrics.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as metrics_file:
            spec = json.load(metrics_file)
        return _shape_table(spec, qc_dir)
    except (OSError, ValueError) as exc:
        return {'error': f'metrics.json could not be read: {exc}'}
