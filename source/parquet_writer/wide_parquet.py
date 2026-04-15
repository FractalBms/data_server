#!/usr/bin/env python3
"""
wide_parquet.py — pivot long parquet files to wide format for inspection

Column name is built from available fields:
  unit signal  : {unit_id}/{device}/{instance}/{point_name}
  site device  : {device}/{instance}/{point_name}
  root signal  : {device}/{point_name}

Usage:
  python3 wide_parquet.py                      # latest file from k3s pod, 200 rows
  python3 wide_parquet.py 50                   # first 50 rows
  python3 wide_parquet.py --all                # all files
  python3 wide_parquet.py --pod [namespace]    # k3s pod (default: data-capture)
  python3 wide_parquet.py --dir <path>         # local directory
  python3 wide_parquet.py --col <col>=<val>    # filter before pivot
"""

import subprocess, sys, os, tempfile, glob, argparse
import pyarrow.parquet as pq
import pyarrow.compute as pc


def fetch_from_pod(namespace):
    result = subprocess.run(
        ['kubectl', 'get', 'pod', '-n', namespace,
         '-l', 'app=parquet-writer',
         '-o', 'jsonpath={.items[0].metadata.name}'],
        capture_output=True, text=True
    )
    pod = result.stdout.strip()
    if not pod:
        sys.exit(f'No parquet-writer pod found in namespace {namespace}')
    tmp = tempfile.mkdtemp()
    dest = os.path.join(tmp, 'data')
    subprocess.run(['kubectl', 'cp', f'{namespace}/{pod}:/data', dest], check=True)
    return sorted(glob.glob(f'{dest}/**/*.parquet', recursive=True))


def fetch_from_dir(path):
    return sorted(glob.glob(f'{path}/**/*.parquet', recursive=True))


def make_sig_name(row):
    """Build signal column name from parsed topic fields."""
    device    = row.get('device') or ''
    instance  = row.get('instance') or ''
    point     = row.get('point_name') or ''
    unit_id   = row.get('unit_id') or ''

    if instance and instance not in ('root', '_'):
        leaf = f'{device}/{instance}/{point}'
    else:
        leaf = f'{device}/{point}'

    if unit_id:
        return f'{unit_id}/{leaf}'
    return leaf


def pivot_table(t, filters):
    """Convert arrow table to wide dict-of-columns."""
    # apply filters
    for col, val in filters:
        if col in t.schema.names:
            t = t.filter(pc.equal(t.column(col), val))

    if len(t) == 0:
        return None, []

    # build rows as dicts
    cols = t.schema.names
    rows_raw = [{c: t.column(c)[i].as_py() for c in cols} for i in range(len(t))]

    # pivot: key = (ts, site_id), signal columns from make_sig_name
    from collections import defaultdict, OrderedDict
    pivoted = OrderedDict()
    sig_cols = []

    for r in rows_raw:
        site = r.get('site_id') or ''
        key = (r.get('ts'), site)
        sig = make_sig_name(r)
        val = r.get('value') if r.get('value') is not None else r.get('value_str')

        if key not in pivoted:
            row_base = {'ts': r.get('ts')}
            if site:
                row_base['site_id'] = site
            pivoted[key] = row_base
        pivoted[key][sig] = val

        if sig not in sig_cols:
            sig_cols.append(sig)

    return list(pivoted.values()), sorted(sig_cols)


def dump_wide(path, max_rows, filters):
    t = pq.read_table(path)
    name = os.path.basename(path)

    rows, sig_cols = pivot_table(t, filters)
    if rows is None:
        print(f'\n=== {name} — no rows match filters ===')
        return

    total = len(rows)
    rows  = rows[:max_rows]
    shown = len(rows)

    print(f'\n=== {name}  ({total} wide rows total, showing {shown}) ===')
    print(f'    signals ({len(sig_cols)}): {sig_cols}\n')

    # display columns: ts, site_id (only if present), then signals
    has_site = any(r.get('site_id') for r in rows)
    display_cols = ['ts'] + (['site_id'] if has_site else []) + sig_cols

    # compute widths
    def fmt(v):
        if v is None:
            return '-'
        if isinstance(v, float):
            return f'{v:.3f}'
        return str(v)

    col_data = {}
    for c in display_cols:
        col_data[c] = [fmt(r.get(c)) for r in rows]
    widths = {c: max(len(c), max((len(v) for v in col_data[c]), default=0)) for c in display_cols}

    header = '  ' + '  '.join(c.ljust(widths[c]) for c in display_cols)
    sep    = '  ' + '  '.join('-' * widths[c] for c in display_cols)
    print(header)
    print(sep)
    for i in range(shown):
        print('  ' + '  '.join(col_data[c][i].ljust(widths[c]) for c in display_cols))


# --- arg parse ---
ap = argparse.ArgumentParser(
    description='Pivot long parquet files to wide format.',
    formatter_class=argparse.ArgumentDefaultsHelpFormatter,
)
ap.add_argument('rows',    nargs='?', type=int, default=200,
                help='Max wide rows to show per file')
ap.add_argument('--all',   action='store_true',
                help='Show all files (default: latest only)')
ap.add_argument('--pod',   nargs='?', const='data-capture', default=None,
                metavar='NAMESPACE', help='Read from k3s pod')
ap.add_argument('--dir',   metavar='PATH', help='Read from local directory')
ap.add_argument('--col',   action='append', default=[], metavar='COL=VAL',
                help='Filter rows before pivot (repeatable)')
args = ap.parse_args()

filters = []
for f in args.col:
    if '=' not in f:
        sys.exit(f'--col must be COL=VAL, got: {f}')
    k, v = f.split('=', 1)
    filters.append((k, v))

if args.dir:
    files = fetch_from_dir(args.dir)
elif args.pod is not None:
    files = fetch_from_pod(args.pod)
else:
    files = fetch_from_pod('data-capture')

if not files:
    sys.exit('No parquet files found.')

if not args.all:
    files = [files[-1]]

for f in files:
    dump_wide(f, args.rows, filters)
