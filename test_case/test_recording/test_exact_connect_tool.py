import json
from types import SimpleNamespace
import pytest
from target import product_policy


@pytest.fixture(autouse=True)
def clean_policy():
    product_policy.reset_for_tests()
    yield
    product_policy.reset_for_tests()


@pytest.mark.parametrize('bad', ['pid','process','handle','hidden','auto_activate',None])
def test_exact_connect_is_current_identity_not_first_title(monkeypatch,bad):
    import target.server as server
    calls=[]
    backend=SimpleNamespace(
        list_windows=lambda:{'ok':True,'windows':[
            {'handle':11,'process_id':7,'title':'EDRClient','visible':bad!='hidden'},
            {'handle':12,'process_id':7,'title':'EDRClient','visible':True}]},
        process_name_for_pid=lambda _: 'EDRClient.exe',
        connect_window_identity=lambda h,p: calls.append((h,p)) or {'ok':True})
    monkeypatch.setattr(server,'_backend',backend)
    result=json.loads(server.connect(handle=99 if bad=='handle' else 11,
        pid=8 if bad=='pid' else 7,process_name='other.exe' if bad=='process' else 'EDRClient.exe',
        title_re='^EDRClient$',auto_activate=bad=='auto_activate'))
    assert result['ok'] is (bad is None)
    assert calls==([(11,7)] if bad is None else [])


def test_exact_connect_cannot_bypass_active_run_fence(monkeypatch):
    import target.server as server
    monkeypatch.setattr(server,'_backend',object())
    monkeypatch.setattr(server,'_legacy_gui_conflict',lambda _:json.dumps(
        {'ok':False,'code':'product_run_conflict'}))
    assert json.loads(server.connect(handle=11,pid=7,process_name='EDRClient.exe'))['code']=='product_run_conflict'
