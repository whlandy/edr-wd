import json
import pytest
from agent.recording.remote_server_tools import McpServerTools


def tools():
    calls = []
    def call(name, args):
        calls.append((name, args))
        if name == 'begin_product_run':
            return {'ok': True, 'run_id': 'remote-run', 'run_token': 'lease'}
        if name == 'window_binding_state':
            return {'ok': True, 'revision': 7, 'generation': 1, 'state': 'BOUND'}
        return {'ok': True}
    return McpServerTools(call), calls


def test_explicit_null_and_json_shape_are_preserved():
    t, calls = tools()
    result = t.focus_bound_window('W1', 'WB1', 'lease', action_id=None, observe=True)
    assert json.loads(result)['ok']
    assert calls[-1][1]['action_id'] is None and calls[-1][1]['observe'] is True
    assert isinstance(t.observe_product_windows('lease'), str)


def test_full_digest_and_actual_remote_authority():
    t, calls = tools()
    t.begin_product_run('windows_hisec', 'a'*64)
    assert calls[0][1]['digest'] == 'a'*64
    context = t._binding_facade_holder['facade'].step_context(
        run_id='remote-run', step_id='s', attempt_id='a', window_id='W1', binding_token='WB1')
    assert context['bindingRevision'] == 7
    assert calls[-1][0] == 'window_binding_state'
    assert calls[-1][1]['run_token'] == 'lease'


def test_foreign_run_rejected_before_remote_call_and_end_clears_view():
    t, calls = tools(); t.begin_product_run('p','d')
    view = t._binding_facade_holder['facade']
    with pytest.raises(ValueError, match='run_mismatch'):
        view.step_context(run_id='foreign', step_id='s', attempt_id='a',window_id='W1',binding_token='WB1')
    assert len(calls) == 1
    t.end_product_run('lease')
    assert t._binding_facade_holder == {}
    with pytest.raises(ValueError):
        view.step_context(run_id='remote-run',step_id='s',attempt_id='a',window_id='W1',binding_token='WB1')


def test_transport_error_is_not_an_empty_inventory_or_retried_action():
    calls = []
    def broken(name, args):
        calls.append(name)
        raise ConnectionError('response lost')
    t = McpServerTools(broken)
    result = json.loads(t.focus_bound_window('W1','WB','lease'))
    assert result['ok'] is False and result['executed'] == 'unknown'
    assert calls == ['focus_bound_window']
    with pytest.raises(ValueError, match='malformed'):
        McpServerTools(lambda *_: None).observe_product_windows('lease')


def test_readonly_flag_survives_transport():
    t, calls = tools()
    t.focus_bound_window('W1','WB','lease',action_id=None,observe=True,read_only=True)
    assert calls[-1][1]['read_only'] is True
