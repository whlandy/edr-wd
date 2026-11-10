"""A/B protocol helpers. Importing does not connect to or operate a desktop.

The caller supplies the real MCP call and current-menu observer; no cached
screenshots/coordinates are interpreted as current facts. B is run separately
after restoring the SAME observed initial state, never as an automatic fallback.
"""


def _idle(status):
    # A freshly restarted server has no session, rather than an idle receipt.
    return (status.get('ok') is True and status.get('state') in {'idle', 'stopped', 'expired'}) or (
        status.get('ok') is False and status.get('code') == 'recording_session_missing')


def run_server_arm(call, observe_menu, *, pid, process_name, x, y, allow_bind=False):
    """One guarded A arm; binding/focus requires explicit caller authority."""
    evidence = {}
    def blocked(reason):
        return {"outcome": "blocked", "reason": reason, "evidence": evidence}
    status = call('recording_status', {'heartbeat': False})
    evidence['recording_status'] = status
    if not _idle(status):
        return blocked('recording_not_idle')
    lock_result = call('get_window_lock', {})
    evidence['initial_lock'] = lock_result
    if lock_result.get('ok') is not True:
        return blocked('lock_unavailable')
    if lock_result.get('locked') is False:
        if not allow_bind:
            return blocked('binding_authorization_required')
        connected = call('connect', {'pid': pid, 'process_name': process_name,
                                     'auto_activate': False})
        evidence['connect'] = connected
        if connected.get('ok') is not True:
            return blocked('connect_failed')
        bound = call('lock_window', {'pid': pid, 'process_name': process_name,
                                     'strict': True, 'activate': True})
        evidence['bind'] = bound
        if bound.get('ok') is not True:
            return blocked('bind_failed')
        lock_result = call('get_window_lock', {})
        evidence['bound_lock'] = lock_result
    lock = lock_result.get('lock') or {}
    if (lock_result.get('ok') is not True or lock_result.get('locked') is not True
            or lock.get('pid') != pid or not lock.get('handle')
            or str(lock.get('process_name', '')).casefold() != process_name.casefold()):
        return blocked('lock_identity_mismatch')
    evidence['target'] = {'pid': pid, 'process_name': process_name.casefold(),
                          'handle': lock['handle'], 'x': x, 'y': y}
    before = observe_menu()
    evidence['before'] = before
    if type(before.get('menu_open')) is not bool or not before.get('observation_ref'):
        return blocked('baseline_unknown')
    # Live observation: clicking an open setting menu can recreate it. Do not
    # assume toggling; both arms must test the same calibrated closed -> open.
    if before['menu_open'] is not False:
        return blocked('baseline_requires_closed_menu')
    # Recheck recording immediately before dispatch. The server performs the
    # authoritative lock/process/occlusion checks; this helper never bypasses it.
    status = call('recording_status', {'heartbeat': False})
    if not _idle(status):
        evidence['pre_dispatch_recording_status'] = status
        return blocked('recording_not_idle')
    receipt = call('click_at', {'x': x, 'y': y, 'expected_process_name': process_name})
    evidence['receipt'] = receipt
    if receipt.get('event_dispatched') is False:
        return blocked('not_dispatched')
    if receipt.get('ok') is not True or receipt.get('event_dispatched') is not True:
        return {'outcome': 'unknown', 'reason': 'dispatch_unconfirmed', 'evidence': evidence}
    after = observe_menu()
    evidence['after'] = after
    if type(after.get('menu_open')) is not bool or not after.get('observation_ref'):
        return {'outcome': 'unknown', 'reason': 'effect_unobserved', 'evidence': evidence}
    return {'outcome': 'pass' if after['menu_open'] else 'fail',
            'reason': 'menu_opened' if after['menu_open'] else 'no_menu_effect',
            'evidence': evidence}


def compare_arms(a, b):
    """Comparability is independent of either arm's UI success."""
    for arm in (a, b):
        if arm.get('outcome') not in {'pass', 'fail'}:
            return {'comparable': False, 'reason': 'arm_not_executed_or_unobserved'}
    if a['evidence']['before']['menu_open'] != b['evidence']['before']['menu_open']:
        return {'comparable': False, 'reason': 'different_initial_menu_state'}
    if not a['evidence'].get('target') or a['evidence']['target'] != b['evidence'].get('target'):
        return {'comparable': False, 'reason': 'different_target_or_point'}
    return {'comparable': True, 'a': a['outcome'], 'b': b['outcome']}
