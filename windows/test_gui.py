import sys
print('Python:', sys.version)

# Try connecting directly to the hisec process by PID
from pywinauto import Application

PIDS = {
    'HiSecEndpointAgent': 6344,
    'EDRClient': 6816,
}

for name, pid in PIDS.items():
    try:
        app = Application(backend='win32').connect(process=pid)
        print(f'\n=== {name} (PID {pid}) windows ===')
        for w in app.windows():
            try:
                if w.is_visible() and w.window_text():
                    print(f'  TEXT: {repr(w.window_text())} | CLASS: {w.class_name()}')
            except Exception as e:
                print(f'  Error: {e}')
    except Exception as e:
        print(f'{name}: {e}')
