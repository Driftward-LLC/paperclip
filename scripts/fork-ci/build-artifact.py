#!/usr/bin/env python3
"""Archive only built package outputs and synthetic Runner test executables."""
from pathlib import Path
import subprocess
import tarfile


def outputs(root):
    manifests = subprocess.check_output(['git', 'ls-files', '-z', '*package.json'], cwd=root).decode().split('\0')
    for manifest in filter(None, manifests):
        package = Path(manifest).parent
        for name in ['dist', 'dist-browser', 'dist-runtime', 'dist-issue-thread', 'provider-assets']:
            output = package / name
            if (root / output).is_dir() and not (root / output).is_symlink():
                yield output
    target = Path('packages/paperclip-runner/runner/target')
    for binary in ['paperclip-runnerd', 'conformance-tracer', 'fake-harness', 'fake-codex-app-server', 'fake-acpx-sidecar']:
        output = target / 'debug' / binary
        if not (root / output).is_file():
            raise ValueError(f'Missing built Runner test executable: {output}')
        yield output
    yield target / 'release/paperclip-runnerd'


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[2]
    with tarfile.open(root / 'workspace-build.tar.gz', 'w:gz', dereference=False) as archive:
        for output in sorted(set(outputs(root))):
            archive.add(root / output, arcname=str(output))
