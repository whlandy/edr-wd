"""Real live failure shape: UP stamps Explorer, HIDE is still queued."""
import pytest
from target.recording.models import CaptureScope
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator
from test_case.test_recording.test_close_race_and_c08_retro import _ClosingDialogResolver

pytestmark=pytest.mark.unit
SCOPE=CaptureScope(backend='windows_pywinauto',process_name='EDRClient.exe',window_title='logo1',target='target')

class Resolver(_ClosingDialogResolver):
    alive=True
    def element_at(self,x,y,pid=None):
        return dict(controlType='Button' if self.alive is True else 'Pane',
            automationId='LogCenterWindow.safraTitleBar.titleWidget.closeBtn' if self.alive is True else '',
            rect=[1241,160,1256,175],protected=False,ownerPid=1984 if self.alive is True else 3456)
    def native_window_is_alive(self,handle,pid):
        assert (handle,pid)==(1115424,1984)
        return self.alive

def packet(kind,ms,window):
    return HookPacket(kind=kind,monotonic_ms=ms,wall_time='2026-11-10T00:00:00.000Z',
        screen_point=(1248,167),button='left',native={'window':window})

@pytest.mark.parametrize('alive,kept',[(False,True),(True,False),(None,False)])
def test_release_native_stamp_of_desktop_does_not_discard_captured_press(alive,kept):
    r=Resolver();c=WindowsUIACorrelator(r);c.seed_scope(SCOPE)
    show=HookPacket(kind='window_transition',monotonic_ms=900,wall_time='2026-11-10T00:00:00.000Z',
        screen_point=None,button=None,native=dict(kind='opened',nativeKind='shown',
        processName='EDRClient.exe',title='Log Center',pid=1984,handle=1115424))
    c.correlate(show,SCOPE,1)
    down={'handle':1115424,'pid':1984,'windowTitle':'Log Center','processName':'EDRClient.exe'}
    c.correlate(packet('pointer_down',1000,down),SCOPE,2)
    # No HIDE/DESTROY has been processed. UIA now answers the desktop behind it.
    r.alive=alive
    if alive is True:
        r.element_at=lambda *a,**kw:dict(controlType='Pane',automationId='',
            rect=[0,0,1440,900],protected=False,ownerPid=3456)
    up={'handle':131398,'pid':3456,'windowTitle':'Program Manager','processName':'explorer.exe'}
    event=c.correlate(packet('pointer_up',1080,up),SCOPE,2)
    assert (event is not None) is kept
    if kept:
        assert event.type=='pointer_click'
        assert event.observed_target.automation_id=='LogCenterWindow.safraTitleBar.titleWidget.closeBtn'
        assert event.evidence['window']['handle']==1115424
        assert c.out_of_scope_events==0
    else:
        assert c.rejections[-1]['reason']=='target_outside_scope'
