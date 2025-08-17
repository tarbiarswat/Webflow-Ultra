# recorder/hotkey.py
import threading
import keyboard  # requires admin on Windows sometimes

class StopSignal:
    def __init__(self):
        self._flag = False
        self._lock = threading.Lock()

    @property
    def triggered(self) -> bool:
        with self._lock:
            return self._flag

    def trigger(self):
        with self._lock:
            self._flag = True

def attach_hotkey(hotkey: str, signal: StopSignal):
    def _cb():
        signal.trigger()
        # print kept minimal; main loop will notice
        print("\n🛑 Stop hotkey detected.")

    t = threading.Thread(
        target=lambda: keyboard.add_hotkey(hotkey, _cb), daemon=True
    )
    t.start()
    return t
