import json
import hashlib
import subprocess
import shutil
from pathlib import Path
import pytest
from publication import identity, atomic, lock, validated, publish
from teddychart.flow import Flow
REAL_POPEN = subprocess.Popen


@pytest.mark.parametrize('day,opening,closing', [('2026-01-06','14:30','21:00'),('2026-07-06','13:30','20:00'),('2026-11-27','14:30','18:00')])
def test_session_window(day,opening,closing):
    from session_bars import rth_bars
    rows=[{'ts':f'{day}T{h}:00Z'} for h in ('13:00',opening,closing,'22:00')]
    assert rth_bars(rows,day)==[rows[1]]


def test_trade_gate_and_required_checker(monkeypatch):
    import command_center as cc
    assert cc._check_cards({'setups':[]},{'verdict':'stand aside'})==''
    assert 'maximum 0' in cc._check_cards({'setups':[{}]},{'verdict':'stand aside'})
    assert 'maximum 1' in cc._check_cards({'setups':[{},{}]},{'verdict':'thin','scenarios_carried':1})
    monkeypatch.setattr(cc,'_read',lambda p:'{}')
    monkeypatch.setattr(cc,'_tape_summary',lambda t:'')
    monkeypatch.setattr(cc,'_state_doc',lambda:None)
    with pytest.raises(RuntimeError,match='Matching state'):
        cc.regenerate_commentary(send=lambda m: pytest.fail('must not call model'))
    monkeypatch.setattr(cc,'_state_doc',lambda:{'trade_gate':{'verdict':'stand aside'}})
    monkeypatch.setattr(cc,'_save_reply',lambda *a,**k:None)
    monkeypatch.setattr(cc,'_log',lambda *a:None)
    monkeypatch.setattr(cc,'_structure_check',lambda *a:('',[]))
    import interpret
    def fail(*a): raise ValueError('broken validator')
    monkeypatch.setattr(interpret,'verify_commentary',fail)
    with pytest.raises(RuntimeError,match='Required narrative checker failed'):
        cc.regenerate_commentary(send=lambda m:json.dumps(dict(lead='Stand aside',reading=[],setups=[])))


def test_actual_staged_build_and_zero_cards(tmp_path,monkeypatch):
    # Explicit exception: only the local, copied renderer runs, in a temporary directory.
    monkeypatch.setattr(subprocess,'Popen',REAL_POPEN)
    root=Path(__file__).resolve().parents[1]
    for name in ('build.py','publication.py','ladder_frag.html','trinity.json','state.json'):
        shutil.copy2(root/name,tmp_path/name)
    t=json.loads((tmp_path/'trinity.json').read_text(encoding='utf-8'))
    publish(tmp_path,dict(lead='Stand aside fixture',reading=['No setup'],setups=[],vex_note=''),t)
    assert validated(tmp_path)
    page=(tmp_path/'trinity.html').read_text(encoding='utf-8')
    assert 'Stand aside fixture' in page and 'data-entry=' not in page
    before=(tmp_path/'build.py').read_bytes()
    publish(tmp_path,dict(lead='Second revision',reading=[],setups=[],vex_note=''),t)
    assert (tmp_path/'build.py').read_bytes()==before
    assert len(list((tmp_path/'notes'/'commentary-revisions').glob('*/*/narrative.json')))==2


def test_input_change_during_render_preserves_old_html(tmp_path,monkeypatch):
    t={'asof':'old'};(tmp_path/'trinity.json').write_text(json.dumps(t))
    (tmp_path/'trinity.html').write_text('old report')
    def render(*a,**kw):
        (Path(kw['cwd'])/'trinity.html').write_text('new report')
        (tmp_path/'trinity.json').write_text('{"asof":"new"}')
    monkeypatch.setattr(subprocess,'run',render)
    with pytest.raises(RuntimeError,match='during rendering'):
        publish(tmp_path,{},t)
    assert (tmp_path/'trinity.html').read_text()=='old report'


def test_flow_unknown_and_completed_seconds():
    f=Flow(); f.reset(0)
    f.trade(1000,100,10,1); f.trade(2000,102,4,2); f.trade(3000,101,6,0)
    f.trade(4001,103,500,1)
    s=f.snapshot(4500); w=s['windows']['10']
    assert (w['delta'],w['unknown'],w['count'],w['coverage']) == (6,6,3,0.7)
    assert w['displacement']==1
    assert s['cvd']==506
    f.trade(1000,105,10,1)
    assert f.snapshot(4500)['out_of_order_dropped']==1
    assert f.snapshot(400000)['windows']['10']['coverage'] is None


def test_ofi_queue_price_changes_and_reset():
    f=Flow();f.book(1000,[(100,10)],[(101,12)])
    f.book(2000,[(100,15)],[(101,8)])
    assert f.snapshot(3000)['windows']['10']['ofi']==9
    f.book(3000,[(100.25,4)],[(101.25,7)])
    assert f.snapshot(4000)['windows']['10']['ofi']==21
    f.reset(4000)
    assert f.snapshot(5000)['windows']['10']['ofi'] is None


def test_snapshot_and_publication_fail_closed(tmp_path):
    assert identity({'a':1,'b':2})==identity({'b':2,'a':1})
    assert not validated(tmp_path)
    t={'asof':'old'}; atomic(tmp_path/'trinity.json',json.dumps(t).encode())
    (tmp_path/'notes').mkdir()
    with lock(tmp_path):
        with pytest.raises(RuntimeError):
            with lock(tmp_path): pass
    with pytest.raises(RuntimeError,match='Inputs changed'):
        publish(tmp_path,{}, {'asof':'new'})
    atomic(tmp_path/'trinity.html',b'ok');atomic(tmp_path/'narrative.json',b'{}')
    atomic(tmp_path/'publication.json',json.dumps(dict(status='passed',snapshot_id=identity(t),
        html_sha256=hashlib.sha256(b'ok').hexdigest(),narrative_sha256=hashlib.sha256(b'{}').hexdigest())).encode())
    assert validated(tmp_path)
    atomic(tmp_path/'trinity.html',b'changed')
    assert not validated(tmp_path)
