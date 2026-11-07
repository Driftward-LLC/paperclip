#!/usr/bin/env python3
"""Use every upstream full-unit matrix row, without incremental selection."""
import json
import os
from pathlib import Path


def plan(matrix):
    result = []
    for key, mode in [('general_tests', 'general'), ('verify_serialized_server', 'serialized')]:
        rows = matrix[key]
        if not rows:
            raise ValueError('Required test matrix cannot be empty')
        for row in rows:
            args = ['--mode', mode]
            if mode == 'general':
                args += ['--group', row['group']]
            if 'shard_count' in row:
                args += ['--shard-index', str(row['shard_index']), '--shard-count', str(row['shard_count'])]
            result.append({'label': mode + ' ' + row.get('group_label', row.get('shard_label', 'workspace')),
                           'args': args})
    return {'include': result}


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2]
    matrix = plan(json.loads((root / '.github/ci-task-matrix.json').read_text()))
    with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
        output.write('matrix=' + json.dumps(matrix, separators=(',', ':')) + '\n')
