"""Docker（headless）纯 Python 运行时 —— PySide6 的极简等价物。

本文件为 Docker 独立副本专用（桌面 Qt 版是另一份独立代码，互不影响），
因此不再保留双运行时分支：容器内永远没有 Qt，直接提供纯线程实现。

导出：
  - Signal           ：类属性描述符，实例级槽列表，connect/disconnect/emit
                       与 Qt Signal 语义对齐（emit 同步直连、幂等 connect）
  - QObject          ：空基类，仅承接 super().__init__() 调用
  - QThread          ：线程持有者。与 threading.Thread 不同，它允许
                       「同一对象 stop → start 再启动」（对齐 QThread 语义，
                       Scheduler.run() 自身已做再入复位），wait() 参数为毫秒
"""
from __future__ import annotations

import threading


class Signal:
    """Qt Signal 的纯 Python 兼容描述符。

    用法与 Qt 一致：
        class T:
            changed = Signal(str, int)
        t = T()
        t.changed.connect(fn)   # 幂等
        t.changed.emit("a", 1)
        t.changed.disconnect(fn)
    """

    def __init__(self, *_types) -> None:
        self._store_attr = ""

    def __set_name__(self, owner, name) -> None:
        self._store_attr = f"__signal_{name}"

    def __get__(self, obj, objtype=None):
        if obj is None:  # 类访问（兼容 Qt 常见写法）
            return self
        store = obj.__dict__.setdefault(self._store_attr, [])
        return _BoundSignal(store)


class _BoundSignal:
    __slots__ = ("_slots",)

    def __init__(self, slots: list) -> None:
        self._slots = slots

    def connect(self, fn) -> None:
        if fn not in self._slots:  # 幂等连接
            self._slots.append(fn)

    def disconnect(self, fn=None) -> None:
        if fn is None:
            self._slots.clear()
        elif fn in self._slots:
            self._slots.remove(fn)

    def emit(self, *args) -> None:
        # tuple 快照：槽内增删连接不影响本轮迭代
        for fn in tuple(self._slots):
            fn(*args)


class QObject:
    """QObject 空基类（纯运行时无线程亲和性/事件循环概念）。"""

    def __init__(self, *args, **kwargs) -> None:
        pass


class QThread:
    """QThread 等价物：线程持有者，支持同实例反复 start()。

    与 QThread 的行为对齐点：
      - start()            异步执行 run()（重复 start 运行中的实例为空操作）
      - wait(ms)           等待结束；参数为毫秒，None=无限等待；返回是否已结束
      - isRunning()        工作线程是否存活
      - requestInterruption() / isInterruptionRequested()
        若子类持有 _stop_event（Scheduler 即是），直接复用它；
        否则使用内置 _interrupt 事件。
      - msleep(ms)/sleep(sec) 静态方法
    """

    def __init__(self, *args, **kwargs) -> None:
        self._runner: threading.Thread | None = None
        self._runner_lock = threading.Lock()
        self._interrupt = threading.Event()

    # ---- 生命周期 ----
    def start(self) -> None:
        with self._runner_lock:
            if self._runner is not None and self._runner.is_alive():
                return
            self._interrupt.clear()
            t = threading.Thread(
                target=self.run,
                name=self.__class__.__name__,
                daemon=True,
            )
            self._runner = t
        t.start()

    def run(self) -> None:  # 子类覆盖
        pass

    def wait(self, ms: int | float | None = None) -> bool:
        t = self._runner
        if t is None:
            return True
        timeout = None if ms is None else float(ms) / 1000.0
        t.join(timeout)
        return not t.is_alive()

    def isRunning(self) -> bool:
        t = self._runner
        return bool(t is not None and t.is_alive())

    # ---- 中断（优先复用子类自有的 _stop_event 实例属性）----
    def requestInterruption(self) -> None:
        self._interrupt.set()
        evt = getattr(self, "_stop_event", None)
        if isinstance(evt, threading.Event):
            evt.set()
        # 唤醒可能阻塞在 pause wait 上的主循环
        pause_evt = getattr(self, "_pause_event", None)
        if isinstance(pause_evt, threading.Event):
            pause_evt.set()

    def isInterruptionRequested(self) -> bool:
        if self._interrupt.is_set():
            return True
        evt = getattr(self, "_stop_event", None)
        return bool(isinstance(evt, threading.Event) and evt.is_set())

    @staticmethod
    def msleep(ms: int) -> None:
        import time
        time.sleep(max(0.0, float(ms) / 1000.0))

    @staticmethod
    def sleep(sec: float) -> None:
        import time
        time.sleep(max(0.0, float(sec)))
