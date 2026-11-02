"""Recorder focuses an existing selected HWND without App Paths or launch."""
import sys
from types import SimpleNamespace

from target.automation.windows_pywinauto import WindowsPywinautoBackend
from target.recording.ui_controller import RecorderUIController


def test_real_windows_focus_never_needs_app_paths(monkeypatch):
    focused = []
    win = SimpleNamespace(process_id=lambda: 123, window_text=lambda: 'Fixture',
                          set_focus=lambda: focused.append(True))
    monkeypatch.setitem(sys.modules, 'pywinauto', SimpleNamespace(
        Desktop=lambda **kw: SimpleNamespace(window=lambda **kw:
            SimpleNamespace(wrapper_object=lambda: win))))
    backend = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    backend._gui = SimpleNamespace(process_name_for_pid=lambda pid: 'Fixture.exe')
    result = backend.focus_existing_window(handle=456,pid=123,title='Fixture',process_name='Fixture.exe')
    assert result['ok'] and focused == [True]
    result = backend.focus_existing_window(handle=456,pid=999,title='Fixture',process_name='Fixture.exe')
    assert not result['ok'] and focused == [True]


def test_panel_prefers_exact_window_focus_and_propagates_failure():
    calls=[]
    backend = SimpleNamespace(
        list_windows=lambda: {'ok':True,'windows':[{'title':'Fixture','process_id':123,
            'process_name':'Fixture.exe','handle':456,'visible':True}]},
        activate_app=lambda **kw: (_ for _ in ()).throw(AssertionError('must not launch')),
        focus_existing_window=lambda **kw: (calls.append(kw) or {'ok':False,'error':'focus denied'}))
    service=SimpleNamespace(start=lambda **kw: (_ for _ in ()).throw(AssertionError('must not start')))
    controller=RecorderUIController(service,backend)
    selected=controller.list_recordable_windows()['windows'][0]['selectionId']
    result=controller.start_from_selection(selected,'demo')
    assert not result['ok'] and result['error']=='focus denied'
    assert calls == [{'handle':456,'pid':123,'title':'Fixture','process_name':'Fixture.exe'}]


def test_start_request_accepts_recording_name_without_argument_collision():
    from target.recording.recorder_panel import RecorderPanel
    panel = RecorderPanel.__new__(RecorderPanel)
    requests = []
    panel.on_request = lambda *args: requests.append(args)
    panel._request("start", selectionId="selected", name="user-demo")
    assert requests[0][0] == "start"
    assert requests[0][2] == {"selectionId": "selected", "name": "user-demo"}
