from types import SimpleNamespace

import pytest

from target.automation.native_inventory import native_top_windows


def test_native_popup_is_retained_even_when_uia_hides_it_from_roots():
    parent = SimpleNamespace(handle=11, is_top_level=lambda: True, is_visible=lambda: True)
    popup = SimpleNamespace(handle=22, is_top_level=lambda: False, is_visible=lambda: True)
    seen = []

    def desktop(*, backend):
        seen.append(backend)
        if backend == "win32":
            return SimpleNamespace(windows=lambda: [parent, popup])
        # This UIA root list is intentionally incomplete: popup is nested.
        return SimpleNamespace(windows=lambda: [parent], window=lambda handle:
            SimpleNamespace(wrapper_object=lambda: {11: parent, 22: popup}[handle]))

    assert [w.handle for w in native_top_windows(desktop,
        uia_factory=lambda handle: {11: parent, 22: popup}[handle])] == [11, 22]
    assert seen == ["win32"]


def test_uia_projection_cannot_replace_native_popup_with_same_title_parent():
    def desktop(*, backend):
        return SimpleNamespace(
            windows=lambda: [SimpleNamespace(handle=22, is_visible=lambda: True)],
            window=lambda handle: SimpleNamespace(wrapper_object=lambda:
                SimpleNamespace(handle=11)))
    with pytest.raises(ValueError, match="identity mismatch"):
        native_top_windows(desktop, uia_factory=lambda handle: SimpleNamespace(handle=11))


def test_native_enumeration_failure_is_not_an_empty_success():
    def desktop(**kwargs):
        raise OSError("native desktop unavailable")
    with pytest.raises(OSError, match="native desktop unavailable"):
        native_top_windows(desktop)


def test_stale_uia_visible_popup_is_excluded_by_native_hide():
    native = SimpleNamespace(handle=22, is_visible=lambda: False)
    def project(handle):
        raise AssertionError("hidden native popup must not reach UIA projection")
    assert native_top_windows(lambda **kwargs: SimpleNamespace(windows=lambda: [native]),
        uia_factory=project) == []


def test_native_title_and_geometry_own_window_identity_uia_owns_controls():
    native=SimpleNamespace(handle=22, is_visible=lambda:True,
        window_text=lambda:'EDRClient', process_id=lambda:1984,
        rectangle=lambda:(981,150,1142,254))
    uia=SimpleNamespace(handle=22, window_text=lambda:'',
        rectangle=lambda:(0,0,0,0), element_info=SimpleNamespace(automation_id=''),
        descendants=lambda:['简体中文','English','Español'])
    w=native_top_windows(lambda **kwargs:SimpleNamespace(windows=lambda:[native]),
        uia_factory=lambda handle:uia)[0]
    assert (w.handle,w.process_id(),w.window_text(),w.rectangle()) == (
        22,1984,'EDRClient',(981,150,1142,254))
    assert w.descendants()==['简体中文','English','Español']
