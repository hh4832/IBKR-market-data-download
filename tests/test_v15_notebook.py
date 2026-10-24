"""Offline syntax, API compatibility and default Run All guardrails."""
import ast
import asyncio
import inspect
import json
from pathlib import Path
import socket
import sys
import types

import pytest
import ibkr_history_core as core

ROOT = Path(__file__).resolve().parents[1]
V15 = ROOT / 'IBKR_NQ_ES_TSM_SOXX_1min_Backfill_v1_5.ipynb'


@pytest.mark.parametrize('path', sorted(ROOT.glob('*.ipynb')), ids=lambda p: p.name)
def test_all_notebook_python_syntax(path):
    notebook = json.loads(path.read_text(encoding='utf-8'))
    assert notebook['nbformat'] == 4
    for i, cell in enumerate(notebook['cells']):
        if cell['cell_type'] != 'code':
            continue
        # Existing historical notebook has IPython line magics; check their
        # surrounding Python without running installations or broker operations.
        source = ''.join(cell['source'])
        source = '\n'.join('# ' + line if line.lstrip().startswith(('%', '!'))
                           else line for line in source.splitlines())
        compile(source, f'{path.name}:cell{i}', 'exec', ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)


def test_v15_settings_and_imports_match_core():
    n = json.loads(V15.read_text(encoding='utf-8'))
    fields = inspect.signature(core.Settings).parameters
    for c in n['cells']:
        if c['cell_type'] != 'code':
            continue
        for node in ast.walk(ast.parse(''.join(c['source']))):
            if isinstance(node, ast.ImportFrom) and node.module == 'ibkr_history_core':
                assert all(hasattr(core, name.name) for name in node.names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'Settings':
                assert all(kw.arg in fields for kw in node.keywords)


def test_v15_default_run_all_has_no_broker_or_data_writes(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError('Default notebook must not connect, download or sync')

    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    for name in ('run_backfill', 'probe_indices', 'sync_tree_to_google_drive'):
        monkeypatch.setattr(core, name, forbidden)
    fake_ib = types.ModuleType('ib_async')
    fake_ib.IB = forbidden
    monkeypatch.setitem(sys.modules, 'ib_async', fake_ib)
    namespace = {'display': lambda *args: None}
    n = json.loads(V15.read_text(encoding='utf-8'))
    for i, c in enumerate(n['cells']):
        if c['cell_type'] != 'code':
            continue
        assert c['outputs'] == [] and c['execution_count'] is None
        code = compile(''.join(c['source']), f'v15:cell{i}', 'exec', ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
        result = eval(code, namespace)
        if inspect.isawaitable(result):
            asyncio.run(result)
    assert namespace['ALLOW_CONNECT'] is False
    assert namespace['ALLOW_DOWNLOAD'] is False
    assert namespace['DRIVE_LOCAL_FOLDER'] is None
    assert list(tmp_path.iterdir()) == []
