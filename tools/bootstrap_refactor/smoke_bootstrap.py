"""Run main() with a fake tkinter so bootstrap()'s top level executes once."""
import sys, os, threading, traceback, builtins
from unittest import mock
sys.path.insert(0, os.getcwd())
for m in ["tkinter","tkinter.font","tkinter.messagebox","tkinter.scrolledtext","tkinter.simpledialog","tkinter.ttk","PIL.ImageTk"]:
    sys.modules[m] = mock.MagicMock(name=m)
import tkinter
reached = {"boot": False}
class FakeTk(mock.MagicMock):
    pass
def make_root(*a, **k):
    r = mock.MagicMock(name="root")
    r.winfo_screenwidth.return_value = 1280
    r.winfo_screenheight.return_value = 800
    r.winfo_width.return_value = 1280
    r.winfo_height.return_value = 800
    calls = []
    def after(ms, fn=None, *args):
        if fn is not None and getattr(fn, "__name__", "") in ("bootstrap", "_bootstrap_after_splash"):
            calls.append(fn)
        return "after#1"
    r.after.side_effect = after
    r.after_idle.side_effect = lambda fn=None, *a: after(0, fn)
    def mainloop():
        fn = calls[0]
        if fn.__name__ != "bootstrap":
            cells = dict(zip(fn.__code__.co_freevars, fn.__closure__))
            fn = cells["bootstrap"].cell_contents
        print("SMOKE: running bootstrap", flush=True)
        fn()
        print("SMOKE: bootstrap returned", flush=True)
    r.mainloop.side_effect = mainloop
    return r
tkinter.Tk = make_root
os.environ.setdefault("PIGEON_NO_SPLASH", "1")
import pigeon_0_9
sys.argv = ["pigeon_0_9.py"]
def watchdog():
    import time; time.sleep(150); print("SMOKE: timeout", flush=True); os._exit(3)
threading.Thread(target=watchdog, daemon=True).start()
try:
    rc = pigeon_0_9.main()
    print("SMOKE: main returned", rc, flush=True)
except SystemExit as e:
    print("SMOKE: exit", e, flush=True)
except BaseException:
    traceback.print_exc()
    print("SMOKE: exception", flush=True)
os._exit(0)
