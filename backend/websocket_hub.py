"""WebSocket 广播、进度合并与慢客户端隔离；不依赖应用服务。"""
import asyncio
from collections import OrderedDict, deque

from fastapi import WebSocket


class _WSClientState:
    def __init__(self):
        self.reliable = deque()
        self.latest = OrderedDict()
        self.terminal_keys = OrderedDict()
        self.wakeup = asyncio.Event()
        self.sender: asyncio.Task | None = None


class WSHub:
    _MAX_RELIABLE = 256
    _MAX_LATEST = 256
    _TERMINAL = {"completed", "failed", "skipped", "ready", "cancelled"}

    def __init__(self):
        self.clients: set[WebSocket] = set()
        self._states: dict[WebSocket, _WSClientState] = {}
        self._overflow_count = 0
        # 事件循环只弱引用任务；慢客户端关闭任务需强引用，避免执行前被回收。
        self._closing: set[asyncio.Task] = set()

    def statistics(self) -> dict:
        return {
            "clients": len(self.clients),
            "reliable_queued": sum(len(state.reliable) for state in self._states.values()),
            "progress_queued": sum(len(state.latest) for state in self._states.values()),
            "slow_client_disconnects": self._overflow_count,
        }

    async def connect(self, ws: WebSocket):
        await ws.accept()
        state = _WSClientState()
        self.clients.add(ws)
        self._states[ws] = state
        state.sender = asyncio.create_task(
            self._sender(ws, state), name="websocket-sender")

    def disconnect(self, ws: WebSocket):
        self.clients.discard(ws)
        state = self._states.pop(ws, None)
        if (state is not None and state.sender is not None
                and state.sender is not asyncio.current_task()):
            state.sender.cancel()

    @classmethod
    def _event_key(cls, msg: dict) -> tuple | None:
        message_type = str(msg.get("type") or "")
        if message_type == "task_update":
            task = msg.get("task") or {}
            task_id = task.get("id")
            return (message_type, task_id) if task_id is not None else None
        if message_type.endswith("_progress") or message_type in {
                "library_refresh_progress", "patrol_update"}:
            identity = next((msg.get(name) for name in (
                "job_id", "run_id", "model_id", "task_id")
                if msg.get(name) is not None), "singleton")
            return (message_type, identity)
        return None

    @classmethod
    def _status(cls, msg: dict) -> str:
        task = msg.get("task") if isinstance(msg.get("task"), dict) else {}
        return str(task.get("status") or msg.get("status") or msg.get("state") or "").casefold()

    @classmethod
    def _is_terminal(cls, msg: dict) -> bool:
        return cls._status(msg) in cls._TERMINAL

    def _overflow(self, ws: WebSocket):
        self._overflow_count += 1
        self.disconnect(ws)

        async def close_slow_client():
            try:
                await ws.close(code=1013, reason="WebSocket client is too slow")
            except Exception:
                pass

        task = asyncio.create_task(close_slow_client(), name="websocket-close-slow")
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def _enqueue(self, ws: WebSocket, state: _WSClientState, msg: dict):
        key = self._event_key(msg)
        status = self._status(msg)
        if key is not None and self._is_terminal(msg):
            # 终态先清掉尚未发送的进度；随后进入可靠 FIFO，绝不被进度覆盖。
            state.latest.pop(key, None)
            state.terminal_keys[key] = None
            state.terminal_keys.move_to_end(key)
            while len(state.terminal_keys) > self._MAX_LATEST:
                state.terminal_keys.popitem(last=False)
            if len(state.reliable) >= self._MAX_RELIABLE:
                self._overflow(ws)
                return
            state.reliable.append(msg)
        elif key is not None:
            # retry 的 queued 是同一任务的新生命周期；其余迟到进度不能越过终态。
            if key in state.terminal_keys:
                if status == "queued":
                    state.terminal_keys.pop(key, None)
                else:
                    return
            state.latest[key] = msg
            state.latest.move_to_end(key)
            while len(state.latest) > self._MAX_LATEST:
                state.latest.popitem(last=False)
        else:
            if len(state.reliable) >= self._MAX_RELIABLE:
                self._overflow(ws)
                return
            state.reliable.append(msg)
        state.wakeup.set()

    async def _sender(self, ws: WebSocket, state: _WSClientState):
        try:
            while True:
                if self._states.get(ws) is not state:
                    return
                await state.wakeup.wait()
                while state.reliable:
                    await asyncio.wait_for(
                        ws.send_json(state.reliable.popleft()), timeout=2.0)
                    if self._states.get(ws) is not state:
                        return
                if state.latest:
                    pending = list(state.latest.values())
                    state.latest.clear()
                    for msg in pending:
                        await asyncio.wait_for(ws.send_json(msg), timeout=2.0)
                        if self._states.get(ws) is not state:
                            return
                state.wakeup.clear()
                if state.reliable or state.latest:
                    state.wakeup.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.disconnect(ws)

    async def broadcast(self, msg: dict):
        # 广播只入队，不等待任何一个网络客户端；慢连接不会阻塞任务状态机。
        for ws, state in tuple(self._states.items()):
            self._enqueue(ws, state, msg)

    async def close(self):
        states = list(self._states.items())
        for ws, _state in states:
            self.disconnect(ws)
        # 停机时不等待慢客户端完成关闭握手，避免卡住 lifespan 收尾。
        closing = list(self._closing)
        for task in closing:
            task.cancel()
        await asyncio.gather(*(
            state.sender for _ws, state in states if state.sender is not None
        ), *closing, return_exceptions=True)
