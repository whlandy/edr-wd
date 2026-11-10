"""Real policy/server/executor; OS focus dismisses a synthetic popup."""
import dataclasses
import json
import pytest
from test_case.test_recording.test_v3_real_server_adapter import (
    _compile_golden, _fake_backend_for, _install_fake_os)
from target import product_policy


@pytest.fixture(autouse=True)
def clean():
    product_policy.reset_for_tests()
    import target.server as server
    server._binding_facade_holder['facade'] = None
    yield
    product_policy.reset_for_tests()
    server._binding_facade_holder['facade'] = None


@pytest.mark.parametrize('legacy_post', [False, True])
def test_post_observation_does_not_dismiss_action_popup(monkeypatch, tmp_path, legacy_post):
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden('same_window',clear_verifiers=True)
    first = dataclasses.replace(golden.steps[0], verifiers=({'type':'window_open',
        'expected':{'exists':True,'processName':'EDRClient.exe','title':'EDRClient'},
        'timeoutSeconds':0.01},))
    golden = dataclasses.replace(golden,steps=(first,*golden.steps[1:]))
    backend = _fake_backend_for('same_window')
    backend._windows['200'] = {'process_id':42,'title':'EDRClient',
        'root_control_id':'Popup','visible':False,'foreground':False}
    focus, click = backend.focus_existing_window, backend.click
    calls=[]
    def focus_dismisses(**kw):
        calls.append('focus')
        backend._windows['200']['visible'] = False
        return focus(**kw)
    def click_opens(**kw):
        result = click(**kw)
        backend._windows['200']['visible'] = True
        return result
    backend.focus_existing_window, backend.click = focus_dismisses, click_opens
    _install_fake_os(monkeypatch,backend)
    if legacy_post:
        real_focus = server.focus_bound_window
        def old_post(*args, **kwargs):
            kwargs.pop('read_only', None)
            return real_focus(*args, **kwargs)
        monkeypatch.setattr(server,'focus_bound_window',old_post)
    result=execute_v3_window_stage_real(golden=golden,ledger=AttemptLedger(tmp_path/'h'),
                                      server_tools=server,window_role_map={'W1':'main'})
    if legacy_post:
        assert result['steps'][0]['outcome']=='fail',result
        assert result['latched'] is True and len(calls)==3
        return
    assert result['steps'][0]['outcome']=='pass',result
    assert result['latched'] is False
    assert len(calls)==4  # pre-observe and dispatch only, never post-observe


def test_readonly_flag_cannot_dispatch(monkeypatch):
    import target.server as server
    monkeypatch.setattr(server,'_binding_facade_for_run',lambda _:object())
    result=json.loads(server.focus_bound_window('W1','WB','lease',
                      action_id='gui.click',observe=True,read_only=True))
    assert result['code']=='readonly_action_forbidden' and result['executed'] is False
