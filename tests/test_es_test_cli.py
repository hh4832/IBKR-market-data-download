from types import SimpleNamespace
import ES_single_test as es
import ibkr_history_recovery as engine
import pytest


def window(symbol='ESH5',start='2024-12-10T00:00:00+00:00'):
    return dict(localSymbol=symbol,start_utc=start,duration_seconds=21600,missing_minutes=360)


def test_single_reviewed_window_and_already_recovered():
    p={'windows':[window(),window('ESM5','2025-04-08T00:00:00+00:00')]}
    assert es.select_test_windows(p)==[p['windows'][0]]
    assert es.select_test_windows({'windows':[]})==[]


def test_diverse_batch_never_selects_nq_or_unreviewed_date():
    p={'windows':[window('ESM5','2025-04-08T00:00:00+00:00'),window('ESU6','2026-07-14T00:00:00+00:00'),
                  window('NQM5','2025-04-08T00:00:00+00:00'),window('ESM5','2025-04-09T00:00:00+00:00')]}
    assert len(es.select_test_windows(p,True))==2


def test_unexpected_subdivision_refuses_more_than_cap():
    with pytest.raises(ValueError):es.select_test_windows({'windows':[window()]*2})


def test_already_recovered_preview_does_not_connect_or_write(monkeypatch,tmp_path):
    source=tmp_path/'source';source.mkdir();overlay=tmp_path/'overlay';overlay.mkdir();reports=tmp_path/'reports'
    monkeypatch.setattr(es.staged,'build_current',lambda *a:{'windows':[]})
    monkeypatch.setattr(engine,'main',lambda *a:pytest.fail('No request needed'))
    assert es.main(['--data-root',str(source),'--recovery-root',str(overlay),'--report-root',str(reports)])==0
    assert not reports.exists()
