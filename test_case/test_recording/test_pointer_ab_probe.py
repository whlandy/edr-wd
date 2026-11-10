from scripts.pointer_ab_probe import compare_arms, run_server_arm


def run(*, locked=True, allow_bind=False, receipt=None, after=True, state='idle', before=False):
    calls = []
    lock = {'pid': 7, 'handle': 11, 'process_name': 'EDRClient.exe'}
    bound = [locked]
    def call(name, args):
        calls.append((name, args))
        if name == 'recording_status':
            if state == 'missing':
                return {'ok': False, 'code': 'recording_session_missing'}
            if state == 'error':
                return {'ok': False, 'code': 'some_other_error'}
            return {'ok': True, 'state': state}
        if name == 'get_window_lock':
            return {'ok': True, 'locked': bound[0], 'lock': lock if bound[0] else None}
        if name == 'lock_window':
            bound[0] = True
        if name == 'click_at':
            return receipt if receipt is not None else {'ok': True, 'event_dispatched': True}
        return {'ok': True}
    observations = iter([{'menu_open': before, 'observation_ref': 'before'},
                         {'menu_open': after, 'observation_ref': 'after'}])
    result = run_server_arm(call, lambda: next(observations), pid=7,
                           process_name='EDRClient.exe', x=1148, y=76,
                           allow_bind=allow_bind)
    return result, calls


def test_missing_lock_requires_explicit_binding_authority_no_click():
    result, calls = run(locked=False)
    assert result['outcome'] == 'blocked'
    assert all(name not in {'connect', 'lock_window', 'click_at', 'run_powershell'} for name, _ in calls)


def test_authorized_binding_uses_process_and_guarded_click():
    result, calls = run(locked=False, allow_bind=True)
    assert result['outcome'] == 'pass'
    names = [name for name, _ in calls]
    assert names.index('connect') < names.index('lock_window') < names.index('click_at')
    args = next(args for name, args in calls if name == 'click_at')
    assert args['expected_process_name'] == 'EDRClient.exe'


def test_active_recording_has_zero_clicks():
    result, calls = run(state='recording')
    assert result['outcome'] == 'blocked' and len(calls) == 1


def test_safety_rejection_never_means_injection_failed_or_runs_b():
    result, calls = run(receipt={'ok': False, 'event_dispatched': False})
    assert result['outcome'] == 'blocked' and result['reason'] == 'not_dispatched'
    assert all(name != 'run_powershell' for name, _ in calls)


def test_ok_without_effect_fails_and_without_dispatch_is_unknown():
    assert run(after=False)[0]['outcome'] == 'fail'
    assert run(receipt={'ok': True})[0]['outcome'] == 'unknown'


def test_ab_must_start_from_same_state():
    a = run()[0]
    b = run()[0]
    assert compare_arms(a, b)['comparable']
    b['evidence']['before']['menu_open'] = True
    assert compare_arms(a, b) == {'comparable': False, 'reason': 'different_initial_menu_state'}
    assert not compare_arms(run(locked=False)[0], a)['comparable']


def test_ab_different_point_is_not_comparable():
    a, b = run()[0], run()[0]
    b['evidence']['target']['x'] += 1
    assert compare_arms(a, b)['reason'] == 'different_target_or_point'


def test_open_menu_is_not_assumed_to_toggle():
    result, calls = run(before=True)
    assert result['reason'] == 'baseline_requires_closed_menu'
    assert all(name != 'click_at' for name, _ in calls)


def test_no_session_is_idle_but_other_status_error_is_not():
    assert run(state='missing')[0]['outcome'] == 'pass'
    result, calls = run(state='error')
    assert result['outcome'] == 'blocked' and len(calls) == 1
