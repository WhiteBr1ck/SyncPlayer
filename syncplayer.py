import json
import os
import queue
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from tkinter import BooleanVar, IntVar, PhotoImage, StringVar, filedialog, messagebox

import customtkinter as ctk
from PIL import Image

try:
    import win32file
    import win32pipe
except ImportError:
    win32file = None
    win32pipe = None

try:
    import win32con
    import win32gui
    import win32process
except ImportError:
    win32con = None
    win32gui = None
    win32process = None

try:
    from screeninfo import get_monitors
except ImportError:
    get_monitors = None

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:
    DND_FILES = None
    TkinterDnD = None


APP_NAME = "SyncPlayer"
APP_VERSION = "0.0.6-dev.2"
DPI_SCALE = 1.0
DEBUG_LOG_PATH = None
DEBUG_LOG_LOCK = threading.Lock()
DEFAULT_CONFIG = {
    "display": {
        "mode": "auto",
        "manual_count": 2,
        "fullscreen": True,
        "local_sync": True,
    },
    "resume": {
        "mode": "start_over",
    },
    "sync": {
        "seek_threshold_seconds": 0.06,
        "timepos_throttle_seconds": 0.04,
    },
    "remote": {
        "enabled": False,
        "mode": "host",
        "host": "0.0.0.0",
        "port": 6090,
        "connect_to": "192.168.1.100:6090",
        "strong_sync_seconds": 10.0,
        "correction_interval_seconds": 0.5,
        "correction_threshold_seconds": 0.08,
        "large_drift_threshold_seconds": 0.25,
    },
    "mpv": {
        "mute_followers": True,
        "disable_subtitles": False,
        "start_paused": False,
        "hardware_decoding": True,
        "extra_args": [],
    },
    "ui": {
        "theme": "system",
    },
}


VIDEO_EXTENSIONS = {
    ".mkv",
    ".mp4",
    ".mov",
    ".m2ts",
    ".mts",
    ".ts",
    ".avi",
    ".webm",
    ".wmv",
    ".flv",
}


def debug_log(message):
    global DEBUG_LOG_PATH
    try:
        if DEBUG_LOG_PATH is None:
            DEBUG_LOG_PATH = app_base_dir() / "syncplayer-debug.log"
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{timestamp}] {message}\n"
        with DEBUG_LOG_LOCK:
            with DEBUG_LOG_PATH.open("a", encoding="utf-8") as file:
                file.write(line)
    except Exception:
        pass


@dataclass
class MonitorRect:
    x: int
    y: int
    width: int
    height: int
    name: str = ""


class MpvClient:
    def __init__(self, session_id, index, pipe_name, event_queue, expected_monitor=None):
        self.session_id = session_id
        self.index = index
        self.pipe_name = pipe_name
        self.event_queue = event_queue
        self.expected_monitor = expected_monitor
        self.process = None
        self.handle = None
        self.reader_thread = None
        self.writer_thread = None
        self.command_queue = queue.PriorityQueue()
        self.command_counter = 0
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.time_pos = None
        self.pause = None
        self.seeking = False
        self.fullscreen = None
        self.display_names = []
        self.osd_dimensions = None
        self.ignore = {}

    def connect(self, timeout=8.0):
        if win32file is None:
            raise RuntimeError("缺少 pywin32，无法连接 mpv IPC。请先运行 pip install -r requirements.txt")

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self.handle = win32file.CreateFile(
                    self.pipe_name,
                    win32file.GENERIC_READ | win32file.GENERIC_WRITE,
                    0,
                    None,
                    win32file.OPEN_EXISTING,
                    0,
                    None,
                )
                win32pipe.SetNamedPipeHandleState(
                    self.handle,
                    win32pipe.PIPE_READMODE_BYTE,
                    None,
                    None,
                )
                break
            except Exception:
                time.sleep(0.05)
        else:
            raise TimeoutError(f"无法连接第 {self.index + 1} 个 mpv IPC: {self.pipe_name}")

        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True)
        self.writer_thread.start()
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        self.observe_property(1, "pause")
        self.observe_property(2, "time-pos")
        self.observe_property(3, "seeking")
        self.observe_property(4, "fullscreen")
        self.observe_property(5, "display-names")
        self.observe_property(6, "osd-dimensions")
        self.observe_property(7, "current-window-scale")
        self.observe_property(8, "window-minimized")
        self.observe_property(9, "window-maximized")

    def observe_property(self, observer_id, name):
        self.command(["observe_property", observer_id, name])

    def set_property(self, name, value):
        self.ignore[name] = time.monotonic() + 0.20
        self.command(["set_property", name, value])

    def seek_absolute(self, seconds):
        self.ignore["time-pos"] = time.monotonic() + 0.60
        self.command(["seek", seconds, "absolute", "exact"])

    def quit(self):
        self.command(["quit"])

    def command(self, command):
        priority = 0 if command[:2] == ["set_property", "pause"] else 1
        if command and command[0] == "seek":
            priority = 2
        with self.lock:
            self.command_counter += 1
            counter = self.command_counter
        debug_log(f"P{self.index} CMD_QUEUE priority={priority} {command}")
        self.command_queue.put((priority, counter, command))

    def _writer_loop(self):
        while not self.closed.is_set():
            try:
                _, _, command = self.command_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if command and command[0] == "seek":
                latest_seek = command
                skipped = 0
                retained = []
                while True:
                    try:
                        item = self.command_queue.get_nowait()
                    except queue.Empty:
                        break
                    item_priority, item_counter, item_command = item
                    if item_command and item_command[0] == "seek":
                        latest_seek = item_command
                        skipped += 1
                    else:
                        retained.append(item)
                for item in retained:
                    self.command_queue.put(item)
                if skipped:
                    debug_log(f"P{self.index} CMD_COALESCE seek skipped={skipped} latest={latest_seek}")
                command = latest_seek
            debug_log(f"P{self.index} CMD {command}")
            payload = json.dumps({"command": command}, ensure_ascii=False).encode("utf-8") + b"\n"
            with self.lock:
                if self.handle is not None:
                    try:
                        win32file.WriteFile(self.handle, payload)
                        debug_log(f"P{self.index} CMD_OK {command}")
                    except Exception as exc:
                        debug_log(f"P{self.index} CMD_FAIL {command} error={exc}")

    def close(self, send_quit=True):
        self.closed.set()
        try:
            if send_quit and self.handle is not None:
                self.quit()
        except Exception:
            pass
        try:
            if self.handle is not None:
                win32file.CloseHandle(self.handle)
        except Exception:
            pass

    def _reader_loop(self):
        buffer = b""
        while not self.closed.is_set():
            try:
                if self.handle is None:
                    time.sleep(0.01)
                    continue

                try:
                    _, available, _ = win32pipe.PeekNamedPipe(self.handle, 0)
                except Exception as exc:
                    debug_log(f"P{self.index} IPC_PEEK_FAIL error={exc}")
                    raise

                if not available:
                    time.sleep(0.005)
                    continue

                _, chunk = win32file.ReadFile(self.handle, min(4096, available))
                if not chunk:
                    time.sleep(0.005)
                    continue
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    self._handle_line(line)
            except Exception as exc:
                debug_log(f"P{self.index} IPC_READ_FAIL closed={self.closed.is_set()} error={exc}")
                if not self.closed.is_set():
                    self.event_queue.put((self.session_id, "ipc_closed", self.index, None))
                return

    def _handle_line(self, line):
        try:
            message = json.loads(line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return

        event = message.get("event")
        if event == "shutdown":
            self.event_queue.put((self.session_id, "shutdown", self.index, None))
            return
        if event != "property-change":
            return

        name = message.get("name")
        value = message.get("data")
        now = time.monotonic()
        suppress_broadcast = now < self.ignore.get(name, 0.0)

        changed = True
        old_value = None
        if name == "pause":
            old_value = self.pause
            new_value = bool(value)
            changed = self.pause != new_value
            self.pause = new_value
        elif name == "time-pos":
            if value is None:
                return
            self.time_pos = float(value)
        elif name == "seeking":
            old_value = self.seeking
            new_value = bool(value)
            changed = self.seeking != new_value
            self.seeking = new_value
        elif name == "fullscreen":
            old_value = self.fullscreen
            new_value = bool(value)
            changed = self.fullscreen != new_value
            self.fullscreen = new_value
        elif name == "display-names":
            self.display_names = list(value or [])
            expected_name = self.expected_monitor.name if self.expected_monitor is not None else ""
            debug_log(f"P{self.index} WINDOW_PROP {name}={value} expected={expected_name or '?'}")
            return
        elif name == "osd-dimensions":
            self.osd_dimensions = value
            expected_size = ""
            if self.expected_monitor is not None:
                expected_size = f" expected={self.expected_monitor.width}x{self.expected_monitor.height}"
            debug_log(f"P{self.index} WINDOW_PROP {name}={value}{expected_size}")
            return
        elif name in {"current-window-scale", "window-minimized", "window-maximized"}:
            debug_log(f"P{self.index} WINDOW_PROP {name}={value}")
            return

        if name in {"pause", "seeking", "fullscreen"}:
            debug_log(
                f"P{self.index} EVENT {name}={value} old={old_value} changed={changed} suppress={suppress_broadcast}"
            )

        if suppress_broadcast:
            debug_log(f"P{self.index} DROP {name} reason=ignore")
            return
        if name in {"seeking", "fullscreen"} and not changed:
            debug_log(f"P{self.index} DROP {name} reason=unchanged")
            return

        debug_log(f"P{self.index} QUEUE {name}={value}")
        self.event_queue.put((self.session_id, "property", self.index, {"name": name, "value": value}))


class RemoteJsonPeer:
    def __init__(self, sock, on_message, on_close=None, name="remote"):
        self.sock = sock
        self.on_message = on_message
        self.on_close = on_close
        self.name = name
        self.closed = threading.Event()
        self.lock = threading.Lock()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def send(self, message):
        if self.closed.is_set():
            return False
        try:
            payload = json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n"
            with self.lock:
                self.sock.sendall(payload)
            return True
        except Exception as exc:
            debug_log(f"REMOTE SEND_FAIL peer={self.name} error={exc}")
            self.close()
            return False

    def close(self):
        if self.closed.is_set():
            return
        self.closed.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _read_loop(self):
        buffer = b""
        try:
            while not self.closed.is_set():
                chunk = self.sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        message = json.loads(line.decode("utf-8", errors="replace"))
                    except json.JSONDecodeError:
                        continue
                    self.on_message(self, message)
        except Exception as exc:
            if not self.closed.is_set():
                debug_log(f"REMOTE READ_FAIL peer={self.name} error={exc}")
        finally:
            self.close()
            if self.on_close is not None:
                self.on_close(self)


class RemoteSyncServer:
    def __init__(self, config, get_state):
        self.config = config
        self.get_state = get_state
        self.sock = None
        self.peers = []
        self.lock = threading.Lock()
        self.running = False
        self.accept_thread = None
        self.strong_sync_until = 0.0
        self.strong_sync_thread = None

    def start(self):
        remote = self.config.get("remote", {})
        host = remote.get("host", "0.0.0.0") or "0.0.0.0"
        port = int(remote.get("port", 6090))
        self.stop()
        self.running = True
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(8)
        self.accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.accept_thread.start()
        debug_log(f"REMOTE SERVER_START {host}:{port}")

    def stop(self):
        self.running = False
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None
        with self.lock:
            peers = list(self.peers)
            self.peers.clear()
        for peer in peers:
            peer.close()

    def broadcast(self, message, strong_sync=False):
        if not self.running:
            return
        payload = self._prepare_payload(message)
        debug_log(f"REMOTE BROADCAST type={payload.get('type')} peers={len(self.peers)}")
        with self.lock:
            peers = list(self.peers)
        for peer in peers:
            peer.send(payload)
        if strong_sync:
            self.start_strong_sync()

    def _prepare_payload(self, message, lead_seconds=0.20):
        payload = dict(message)
        now = time.monotonic()
        payload.setdefault("execute_at", now + lead_seconds)
        if payload.get("position") is not None and not bool(payload.get("paused", False)):
            payload["position"] = float(payload["position"]) + max(0.0, float(payload["execute_at"]) - now)
        return payload

    def _send_current_state(self, peer):
        state = self.get_state()
        if state is None or not state.get("path"):
            debug_log(f"REMOTE WELCOME_SKIP peer={peer.name} reason=no-state")
            return
        payload = self._prepare_payload({"type": "open", **state}, lead_seconds=0.50)
        debug_log(f"REMOTE WELCOME peer={peer.name} path={payload.get('path')} position={payload.get('position')} paused={payload.get('paused')}")
        peer.send(payload)
        self.start_strong_sync()

    def start_strong_sync(self):
        seconds = float(self.config.get("remote", {}).get("strong_sync_seconds", 10.0))
        self.strong_sync_until = max(self.strong_sync_until, time.monotonic() + seconds)
        if self.strong_sync_thread is None or not self.strong_sync_thread.is_alive():
            self.strong_sync_thread = threading.Thread(target=self._strong_sync_loop, daemon=True)
            self.strong_sync_thread.start()

    def _strong_sync_loop(self):
        interval = float(self.config.get("remote", {}).get("correction_interval_seconds", 0.5))
        interval = max(0.10, interval)
        while self.running and time.monotonic() < self.strong_sync_until:
            state = self.get_state()
            if state is not None and state.get("position") is not None:
                self.broadcast({"type": "state", **state})
            time.sleep(interval)

    def _accept_loop(self):
        while self.running and self.sock is not None:
            try:
                client_sock, address = self.sock.accept()
                peer = RemoteJsonPeer(client_sock, self._handle_message, self._remove_peer, name=f"{address[0]}:{address[1]}")
                with self.lock:
                    self.peers.append(peer)
                debug_log(f"REMOTE CLIENT_CONNECTED {address[0]}:{address[1]}")
                self._send_current_state(peer)
            except Exception as exc:
                if self.running:
                    debug_log(f"REMOTE ACCEPT_FAIL error={exc}")
                return

    def _remove_peer(self, peer):
        with self.lock:
            if peer in self.peers:
                self.peers.remove(peer)
        debug_log(f"REMOTE CLIENT_CLOSED peer={peer.name}")

    def _handle_message(self, peer, message):
        msg_type = message.get("type")
        if msg_type == "time_sync":
            peer.send({
                "type": "time_sync_response",
                "client_send_time": message.get("client_send_time"),
                "server_time": time.monotonic(),
            })


class RemoteSyncClient:
    def __init__(self, config, schedule_command, get_state):
        self.config = config
        self.schedule_command = schedule_command
        self.get_state = get_state
        self.peer = None
        self.running = False
        self.worker = None
        self.clock_offset = 0.0

    def start(self):
        self.stop()
        self.running = True
        self.worker = threading.Thread(target=self._connect_loop, daemon=True)
        self.worker.start()

    def stop(self):
        self.running = False
        if self.peer is not None:
            self.peer.close()
            self.peer = None

    def _connect_loop(self):
        while self.running:
            try:
                host, port = self._parse_connect_to()
                sock = socket.create_connection((host, port), timeout=5.0)
                sock.settimeout(None)
                self.peer = RemoteJsonPeer(sock, self._handle_message, self._peer_closed, name=f"{host}:{port}")
                debug_log(f"REMOTE CLIENT_CONNECTED_TO {host}:{port}")
                self._time_sync_loop()
            except Exception as exc:
                if self.running:
                    debug_log(f"REMOTE CONNECT_FAIL error={exc}")
                    time.sleep(2.0)

    def _parse_connect_to(self):
        target = self.config.get("remote", {}).get("connect_to", "127.0.0.1:6090")
        if ":" in target:
            host, port = target.rsplit(":", 1)
            return host.strip(), int(port)
        return target.strip(), int(self.config.get("remote", {}).get("port", 6090))

    def _peer_closed(self, peer):
        if self.peer is peer:
            self.peer = None
        debug_log(f"REMOTE CLIENT_DISCONNECTED peer={peer.name}")

    def _time_sync_loop(self):
        while self.running and self.peer is not None and not self.peer.closed.is_set():
            self.peer.send({"type": "time_sync", "client_send_time": time.monotonic()})
            time.sleep(3.0)

    def _handle_message(self, peer, message):
        msg_type = message.get("type")
        debug_log(f"REMOTE CLIENT_RECV type={msg_type}")
        if msg_type == "time_sync_response":
            client_send = message.get("client_send_time")
            server_time = message.get("server_time")
            if client_send is None or server_time is None:
                return
            client_receive = time.monotonic()
            midpoint = (float(client_send) + client_receive) / 2.0
            self.clock_offset = float(server_time) - midpoint
            debug_log(f"REMOTE CLOCK offset={self.clock_offset:.4f}")
            return
        self._schedule_remote_message(message)

    def _schedule_remote_message(self, message):
        execute_at = message.get("execute_at")
        delay = 0.0
        if execute_at is not None:
            local_execute_at = float(execute_at) - self.clock_offset
            delay = max(0.0, local_execute_at - time.monotonic())
        timer = threading.Timer(delay, self._execute_message, args=(message,))
        timer.daemon = True
        timer.start()

    def _execute_message(self, message):
        msg_type = message.get("type")
        debug_log(f"REMOTE CLIENT_EXEC type={msg_type} path={message.get('path')} position={message.get('position')}")
        if msg_type == "state":
            state = self.get_state()
            if state is None or state.get("position") is None or message.get("position") is None:
                return
            if bool(state.get("paused")) != bool(message.get("paused")):
                self.schedule_command({"type": "pause", "paused": bool(message.get("paused")), "remote": True})
                return
            if bool(message.get("paused")):
                return
            drift = abs(float(state["position"]) - float(message["position"]))
            threshold = float(self.config.get("remote", {}).get("large_drift_threshold_seconds", 0.25))
            if drift >= threshold:
                self.schedule_command({"type": "seek", "position": float(message["position"]), "remote": True})
            return
        self.schedule_command(dict(message, remote=True))


class SyncController:
    def __init__(self, base_dir, config):
        self.base_dir = base_dir
        self.config = config
        self.mpv_path = base_dir / "mpv" / "mpv.exe"
        self.event_queue = queue.Queue()
        self.clients = {}
        self.processes = []
        self.process_by_index = {}
        self.running = False
        self.worker = None
        self.last_seek_sync = {}
        self.last_observed_time = None
        self.pending_seek_source_index = None
        self.pending_drag_seek = None
        self.pending_drag_timer = None
        self.post_seek_check_generation = 0
        self.sync_source_index = 0
        self.sync_source_until = 0.0
        self.session_id = 0
        self.current_video_path = None
        self.event_callback = None

    def set_event_callback(self, callback):
        self.event_callback = callback

    def remote_state(self):
        source = self.clients.get(0) or next(iter(self.clients.values()), None)
        if source is None:
            return None
        return {
            "path": str(self.current_video_path) if self.current_video_path is not None else None,
            "position": source.time_pos,
            "paused": bool(source.pause),
            "fullscreen": bool(source.fullscreen),
        }

    def set_pause(self, paused):
        for client in list(self.clients.values()):
            client.set_property("pause", bool(paused))

    def seek_all(self, seconds):
        for client in list(self.clients.values()):
            client.seek_absolute(float(seconds))

    def set_fullscreen(self, fullscreen):
        fullscreen = bool(fullscreen)
        if fullscreen:
            self._apply_all_window_placements(self.session_id, fullscreen=True)
        for index, client in list(self.clients.items()):
            if fullscreen:
                move_mpv_window_to_monitor(client.process, client.expected_monitor, index, fullscreen=True)
            client.set_property("fullscreen", fullscreen)
        if not fullscreen:
            self._schedule_windowed_geometry_fix(self.session_id)

    def apply_remote_command(self, command):
        msg_type = command.get("type")
        if msg_type == "open":
            path = command.get("path")
            if path:
                self.start(Path(path), notify_remote=False)
                position = command.get("position")
                if position is not None and float(position) > 0:
                    self.seek_all(float(position))
                if "paused" in command:
                    self.set_pause(bool(command.get("paused")))
            return
        if msg_type == "pause":
            self.set_pause(bool(command.get("paused")))
            return
        if msg_type == "seek":
            position = command.get("position")
            if position is not None:
                self.seek_all(float(position))
            return
        if msg_type == "fullscreen":
            self.set_fullscreen(bool(command.get("fullscreen")))
            return
        if msg_type == "close":
            self.stop(notify_remote=False)

    def _notify_remote(self, message, strong_sync=False):
        if self.event_callback is None:
            return
        try:
            self.event_callback(message, strong_sync=strong_sync)
        except Exception as exc:
            debug_log(f"REMOTE CALLBACK_FAIL error={exc}")

    def start(self, video_path, notify_remote=True):
        if not self.mpv_path.exists():
            raise FileNotFoundError(f"找不到同目录 mpv: {self.mpv_path}")
        if win32file is None:
            raise RuntimeError("缺少 pywin32，无法使用 mpv IPC。请先运行 pip install -r requirements.txt")

        debug_log(f"START video={video_path}")
        self.stop(notify_remote=False)
        self._clear_events()
        self.session_id += 1
        session_id = self.session_id
        self.current_video_path = Path(video_path)
        monitors = get_display_layout()
        count = output_count(self.config, monitors)
        display_mode = self.config["display"].get("mode")
        fullscreen = bool(self.config["display"].get("fullscreen", True))
        monitor_details = "; ".join(
            f"M{index}={monitor.name or '?'}:{monitor.width}x{monitor.height}+{monitor.x}+{monitor.y}"
            for index, monitor in enumerate(monitors)
        )
        debug_log(
            f"START_LAYOUT mode={display_mode} fullscreen={fullscreen} monitors={len(monitors)} count={count} {monitor_details}"
        )
        if count < 1:
            raise RuntimeError("播放器数量必须至少为 1。")

        self.running = True
        self.pending_seek_source_index = None
        self.pending_drag_seek = None
        self.pending_drag_timer = None
        self.post_seek_check_generation += 1
        self.sync_source_index = 0
        self.sync_source_until = 0.0
        self.last_observed_time = {index: None for index in range(count)}
        self.last_seek_event_at = {index: 0.0 for index in range(count)}
        self.last_seek_sync = {index: 0.0 for index in range(count)}
        pid = os.getpid()
        now = int(time.time() * 1000)
        pipes = []

        for index in range(count):
            pipe_name = fr"\\.\pipe\syncplayer-player-{index}-{pid}-{now}"
            pipes.append(pipe_name)
            self._launch_mpv(index, video_path, pipe_name, monitors, count)

        for index, pipe_name in enumerate(pipes):
            expected_monitor = monitors[index % max(1, len(monitors))]
            client = MpvClient(session_id, index, pipe_name, self.event_queue, expected_monitor)
            client.process = self.process_by_index.get(index)
            client.connect()
            self.clients[index] = client

        if self.config["mpv"].get("disable_subtitles", False):
            for client in self.clients.values():
                client.set_property("sid", "no")
                client.set_property("sub-visibility", False)

        if self.config["mpv"].get("start_paused", False):
            for client in self.clients.values():
                client.set_property("pause", True)

        self._place_all_windows(session_id, fullscreen=False, delay=0.25)
        self._schedule_initial_fullscreen_check(session_id)

        self.worker = threading.Thread(target=self._sync_loop, args=(session_id,), daemon=True)
        self.worker.start()
        if notify_remote:
            self._notify_remote({"type": "open", "path": str(video_path), "position": 0.0, "paused": False}, strong_sync=True)

    def stop(self, notify_remote=True):
        debug_log("STOP")
        was_running = self.running or bool(self.clients) or bool(self.processes)
        self.running = False
        for client in self.clients.values():
            client.close()
        self.clients.clear()
        for process in self.processes:
            if process.poll() is None:
                try:
                    process.terminate()
                    process.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    process.kill()
                except Exception:
                    pass
        self.processes.clear()
        self.process_by_index.clear()
        self.current_video_path = None
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=0.2)
        if notify_remote and was_running:
            self._notify_remote({"type": "close"})

    def _clear_events(self):
        while True:
            try:
                self.event_queue.get_nowait()
            except queue.Empty:
                return

    def _launch_mpv(self, index, video_path, pipe_name, monitors, count):
        fullscreen = bool(self.config["display"].get("fullscreen", True))
        screen_index = index % max(1, len(monitors))
        monitor = monitors[screen_index]
        args = [
            str(self.mpv_path),
            str(video_path),
            f"--input-ipc-server={pipe_name}",
            "--force-window=yes",
            "--idle=no",
            "--keep-open=yes",
            "--osd-on-seek=msg-bar",
        ]

        if self.config["resume"].get("mode") == "remember":
            args.append("--save-position-on-quit=yes")
        else:
            args.extend(["--start=0", "--save-position-on-quit=no"])

        if self.config["mpv"].get("hardware_decoding", True):
            args.extend(["--hwdec=auto-safe", "--hwdec-codecs=all"])

        if self.config["mpv"].get("disable_subtitles", False):
            args.extend(["--sid=no", "--sub-visibility=no", "--sub-auto=no"])

        if fullscreen:
            debug_log(
                f"LAUNCH P{index} fullscreen=True win32_delayed_fs=True monitor={monitor.name or '?'}:{monitor.width}x{monitor.height}+{monitor.x}+{monitor.y} pipe={pipe_name}"
            )
        else:
            debug_log(
                f"LAUNCH P{index} fullscreen=False win32_windowed=True monitor={monitor.name or '?'}:{monitor.width}x{monitor.height}+{monitor.x}+{monitor.y} pipe={pipe_name}"
            )

        if index > 0 and self.config["mpv"].get("mute_followers", True):
            args.append("--mute=yes")
        args.extend(self.config["mpv"].get("extra_args", []))

        launch_args_for_log = [arg for arg in args if not arg.startswith("--input-ipc-server=")]
        debug_log(f"LAUNCH_ARGS P{index} {' | '.join(launch_args_for_log)}")

        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        process = subprocess.Popen(args, cwd=self.base_dir, creationflags=creationflags)
        debug_log(f"LAUNCH_OK P{index} pid={process.pid}")
        self.processes.append(process)
        self.process_by_index[index] = process

    def _place_all_windows(self, session_id, fullscreen=False, delay=0.0):
        if delay <= 0:
            self._apply_all_window_placements(session_id, fullscreen)
            return
        timer = threading.Timer(delay, self._apply_all_window_placements, args=(session_id, fullscreen))
        timer.daemon = True
        timer.start()

    def _apply_all_window_placements(self, session_id, fullscreen=False):
        if not self.running or session_id != self.session_id:
            debug_log(
                f"CTRL PLACE_ALL skipped fullscreen={fullscreen} running={self.running} session={session_id} current={self.session_id}"
            )
            return
        debug_log(f"CTRL PLACE_ALL fullscreen={fullscreen}")
        for index, client in list(self.clients.items()):
            move_mpv_window_to_monitor(client.process, client.expected_monitor, index, fullscreen=fullscreen)

    def _schedule_initial_fullscreen_check(self, session_id):
        if not bool(self.config["display"].get("fullscreen", True)):
            return
        for delay in (0.55, 1.2, 2.2):
            timer = threading.Timer(delay, self._initial_fullscreen_check, args=(session_id, delay))
            timer.daemon = True
            timer.start()

    def _initial_fullscreen_check(self, session_id, delay):
        if not self.running or session_id != self.session_id:
            debug_log(
                f"CTRL FS_CHECK skipped delay={delay} running={self.running} session={session_id} current={self.session_id}"
            )
            return
        debug_log(f"CTRL FS_CHECK delay={delay}")
        for index, client in list(self.clients.items()):
            expected = client.expected_monitor
            expected_name = expected.name if expected is not None else ""
            actual_names = client.display_names or []
            actual_dimensions = client.osd_dimensions or {}
            actual_w = actual_dimensions.get("w") if isinstance(actual_dimensions, dict) else None
            actual_h = actual_dimensions.get("h") if isinstance(actual_dimensions, dict) else None
            on_expected_display = not expected_name or expected_name in actual_names
            has_expected_size = (
                expected is None
                or actual_w in (None, 0)
                or abs(int(actual_w) - expected.width) <= 8
            )
            needs_fix = client.fullscreen is not True or not on_expected_display or not has_expected_size
            debug_log(
                f"CTRL FS_CHECK P{index} fullscreen={client.fullscreen} actual_names={actual_names} "
                f"expected={expected_name or '?'} actual_size={actual_w}x{actual_h} needs_fix={needs_fix}"
            )
            if needs_fix:
                debug_log(f"CTRL FS_FIX P{index} move_then_fullscreen")
                move_mpv_window_to_monitor(client.process, client.expected_monitor, index, fullscreen=True)
                client.set_property("fullscreen", True)

    def _sync_loop(self, session_id):
        while self.running and session_id == self.session_id:
            try:
                event_session_id, event_type, index, payload = self.event_queue.get(timeout=0.02)
                if event_session_id != session_id or session_id != self.session_id:
                    continue
                if event_type == "property":
                    try:
                        self._handle_property(index, payload)
                    except Exception:
                        continue
                elif event_type in {"ipc_closed", "shutdown"}:
                    self._quit_peers(index)
                    self.running = False
                    self._notify_remote({"type": "close"})
            except queue.Empty:
                pass

    def _handle_property(self, index, payload):
        source = self.clients.get(index)
        if source is None:
            debug_log(f"CTRL DROP index={index} reason=no-source payload={payload}")
            return
        name = payload["name"]
        value = payload["value"]
        if time.monotonic() < source.ignore.get(name, 0.0):
            debug_log(f"CTRL DROP P{index} {name}={value} reason=controller-ignore")
            return

        debug_log(f"CTRL HANDLE P{index} {name}={value}")
        if name == "pause":
            is_paused = bool(value)
            position = source.time_pos
            if not is_paused and self.pending_seek_source_index is not None:
                pending_source = self.clients.get(self.pending_seek_source_index)
                if pending_source is not None and pending_source.time_pos is not None:
                    self._broadcast_seek(pending_source, pending_source.time_pos)
                    self.pending_seek_source_index = None
                    self._schedule_play_all(delay_seconds=0.25)
                    return
                self.pending_seek_source_index = None
            self._broadcast_property(index, "pause", is_paused)
            self._notify_remote({"type": "pause", "paused": is_paused, "position": position}, strong_sync=not is_paused)
        elif name == "fullscreen":
            is_fullscreen = bool(value)
            if is_fullscreen:
                self._apply_all_window_placements(self.session_id, fullscreen=True)
            self._broadcast_property(index, "fullscreen", is_fullscreen)
            self._notify_remote({"type": "fullscreen", "fullscreen": is_fullscreen})
            if not is_fullscreen:
                self._schedule_windowed_geometry_fix(self.session_id)
        elif name == "time-pos" and value is not None:
            self._sync_timepos(source, float(value))

    def _schedule_windowed_geometry_fix(self, session_id):
        for delay in (0.25, 0.7):
            timer = threading.Timer(delay, self._apply_windowed_geometry_fix, args=(session_id, delay))
            timer.daemon = True
            timer.start()

    def _apply_windowed_geometry_fix(self, session_id, delay):
        if not self.running or session_id != self.session_id:
            debug_log(
                f"CTRL WIN_FIX skipped delay={delay} running={self.running} session={session_id} current={self.session_id}"
            )
            return
        debug_log(f"CTRL WIN_FIX delay={delay}")
        for index, client in list(self.clients.items()):
            if client.fullscreen is True or client.expected_monitor is None:
                debug_log(f"CTRL WIN_FIX skip P{index} fullscreen={client.fullscreen}")
                continue
            debug_log(f"CTRL WIN_FIX P{index}")
            move_mpv_window_to_monitor(client.process, client.expected_monitor, index, fullscreen=False)

    def _broadcast_property(self, source_index, name, value):
        debug_log(f"CTRL BROADCAST_PROPERTY from=P{source_index} {name}={value}")
        for index, client in self.clients.items():
            if index == source_index:
                continue
            if name == "fullscreen" and value is True:
                move_mpv_window_to_monitor(client.process, client.expected_monitor, index, fullscreen=True)
            if name == "pause" or getattr(client, name, None) != value:
                debug_log(f"CTRL SEND P{index} {name}={value} current={getattr(client, name, None)}")
                client.set_property(name, value)
            else:
                debug_log(f"CTRL SKIP P{index} {name}={value} current={getattr(client, name, None)}")

    def _schedule_play_all(self, delay_seconds=0.0):
        if delay_seconds <= 0:
            self._play_all(self.session_id)
            return
        timer = threading.Timer(delay_seconds, self._play_all, args=(self.session_id,))
        timer.daemon = True
        timer.start()

    def _play_all(self, session_id):
        if not self.running or session_id != self.session_id:
            debug_log(f"CTRL PLAY_ALL skipped running={self.running} session={session_id} current={self.session_id}")
            return
        debug_log("CTRL PLAY_ALL pause=False")
        for client in list(self.clients.values()):
            client.set_property("pause", False)

    def _quit_peers(self, source_index):
        for index, client in self.clients.items():
            if index != source_index:
                try:
                    client.quit()
                except Exception:
                    pass

    def _sync_timepos(self, source, value):
        now = time.monotonic()
        previous = self.last_observed_time.get(source.index)
        self.last_observed_time[source.index] = value
        source.time_pos = value

        threshold = self.config["sync"]["seek_threshold_seconds"]
        user_jump = previous is not None and abs(value - previous) >= max(0.20, threshold * 3)
        if user_jump:
            debug_log(f"CTRL TIMEPOS P{source.index} value={value} previous={previous} pause={source.pause} seeking={source.seeking} user_jump=True")
        if source.pause and user_jump:
            debug_log(f"CTRL PENDING_SEEK source=P{source.index} value={value}")
            self.pending_seek_source_index = source.index
            self.sync_source_index = source.index
            self.sync_source_until = now + 5.0
            self._schedule_post_seek_checks(source.index, value)
            return

        if now - self.last_seek_sync.get(source.index, 0.0) < self.config["sync"]["timepos_throttle_seconds"]:
            return

        should_broadcast = False
        if source.seeking:
            debug_log(f"CTRL SCHEDULE_DRAG_SEEK source=P{source.index} value={value}")
            self._schedule_drag_seek(source, value)
            return

        if should_broadcast:
            self._broadcast_seek(source, value)

    def _schedule_post_seek_checks(self, source_index, target_time):
        self.post_seek_check_generation += 1
        generation = self.post_seek_check_generation
        debug_log(f"CTRL POST_SEEK_SCHEDULE source=P{source_index} target={target_time} gen={generation}")
        for delay in (0.6, 1.2, 2.0, 3.0):
            timer = threading.Timer(
                delay,
                self._post_seek_check,
                args=(self.session_id, generation, source_index, target_time, delay),
            )
            timer.daemon = True
            timer.start()

    def _post_seek_check(self, session_id, generation, source_index, target_time, delay):
        if not self.running or session_id != self.session_id or generation != self.post_seek_check_generation:
            debug_log(
                f"CTRL POST_SEEK_SKIP delay={delay} gen={generation} current_gen={self.post_seek_check_generation} "
                f"running={self.running} session={session_id} current_session={self.session_id}"
            )
            return

        source = self.clients.get(source_index)
        if source is not None and source.time_pos is not None:
            target_time = source.time_pos

        threshold = max(0.12, self.config["sync"].get("seek_threshold_seconds", 0.06) * 2)
        debug_log(f"CTRL POST_SEEK_CHECK delay={delay} source=P{source_index} target={target_time} threshold={threshold}")
        for index, client in list(self.clients.items()):
            if index == source_index or client.time_pos is None:
                continue
            delta = abs(client.time_pos - target_time)
            debug_log(f"CTRL POST_SEEK_DELTA P{index} time={client.time_pos} delta={delta}")
            if delta > threshold:
                debug_log(f"CTRL POST_SEEK_FIX P{index} seek={target_time}")
                client.seek_absolute(target_time)
                self.last_seek_sync[source_index] = time.monotonic()

    def _schedule_drag_seek(self, source, value):
        self.pending_drag_seek = (source.index, value, self.session_id)
        if self.pending_drag_timer is not None:
            self.pending_drag_timer.cancel()
        self.pending_drag_timer = threading.Timer(0.25, self._flush_drag_seek, args=(self.session_id,))
        self.pending_drag_timer.daemon = True
        self.pending_drag_timer.start()

    def _flush_drag_seek(self, session_id):
        if not self.running or session_id != self.session_id or self.pending_drag_seek is None:
            return
        source_index, value, seek_session_id = self.pending_drag_seek
        if seek_session_id != session_id:
            return
        source = self.clients.get(source_index)
        if source is not None:
            self._broadcast_seek(source, value)
            self._schedule_post_seek_checks(source.index, value)
        self.pending_drag_seek = None

    def _broadcast_seek(self, source, value):
        debug_log(f"CTRL BROADCAST_SEEK from=P{source.index} value={value}")
        now = time.monotonic()
        self.sync_source_index = source.index
        self.sync_source_until = now + 3.0
        for index, target in self.clients.items():
            if index != source.index:
                target.seek_absolute(value)
        self.last_seek_sync[source.index] = now
        self._notify_remote({"type": "seek", "position": value, "paused": bool(source.pause)}, strong_sync=True)

def setup_dpi_awareness():
    global DPI_SCALE
    DPI_SCALE = 1.0


def app_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(relative_path):
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / relative_path
    return Path(__file__).resolve().parent / relative_path


def load_config(base_dir):
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    path = base_dir / "syncplayer.json"
    if path.exists():
        with path.open("r", encoding="utf-8") as file:
            deep_update(config, json.load(file))
    migrate_config(config)
    return config


def save_config(base_dir, config):
    with (base_dir / "syncplayer.json").open("w", encoding="utf-8") as file:
        json.dump(config, file, ensure_ascii=False, indent=2)
        file.write("\n")


def deep_update(target, source):
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_update(target[key], value)
        else:
            target[key] = value


def migrate_config(config):
    if "left_screen" in config or "right_screen" in config:
        config.setdefault("display", {})
        config["display"].setdefault("mode", "manual")
        config["display"].setdefault("manual_count", 2)
        config["display"].setdefault("fullscreen", True)
        config["display"].setdefault("local_sync", True)
    if "mute_right" in config.get("mpv", {}):
        config["mpv"]["mute_followers"] = config["mpv"].pop("mute_right")


def get_display_layout():
    if get_monitors is not None:
        monitors = get_monitors()
        if monitors:
            return [MonitorRect(m.x, m.y, m.width, m.height, getattr(m, "name", "")) for m in monitors]

    root = ctk.CTk()
    root.withdraw()
    width = root.winfo_screenwidth()
    height = root.winfo_screenheight()
    root.destroy()
    return [MonitorRect(0, 0, width, height, "")]


def output_count(config, monitors):
    display = config["display"]
    if not bool(display.get("local_sync", True)):
        return 1
    if display.get("mode") == "manual":
        return max(1, int(display.get("manual_count", 2)))
    return max(1, len(monitors))


def find_main_window_for_pid(pid):
    if win32gui is None or win32con is None or win32process is None or pid is None:
        return None
    matches = []

    def enum_callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        try:
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if window_pid != pid:
            return
        if win32gui.GetWindow(hwnd, win32con.GW_OWNER):
            return
        rect = win32gui.GetWindowRect(hwnd)
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        if width > 100 and height > 100:
            matches.append((width * height, hwnd, rect))

    try:
        win32gui.EnumWindows(enum_callback, None)
    except Exception as exc:
        debug_log(f"WIN32 FIND_FAIL pid={pid} error={exc}")
        return None
    if not matches:
        debug_log(f"WIN32 FIND_NONE pid={pid}")
        return None
    matches.sort(reverse=True)
    hwnd = matches[0][1]
    debug_log(f"WIN32 FIND_OK pid={pid} hwnd={hwnd} rect={matches[0][2]}")
    return hwnd


def move_mpv_window_to_monitor(process, monitor, index, fullscreen=False):
    if win32gui is None or win32con is None or process is None or monitor is None:
        debug_log(f"WIN32 MOVE_SKIP P{index} unavailable")
        return
    hwnd = find_main_window_for_pid(process.pid)
    if hwnd is None:
        return
    if fullscreen:
        width = monitor.width
        height = monitor.height
        x = monitor.x
        y = monitor.y
    else:
        width = max(640, int(monitor.width * 0.88))
        height = max(360, int(monitor.height * 0.88))
        x = monitor.x + max(0, (monitor.width - width) // 2)
        y = monitor.y + max(0, (monitor.height - height) // 2)
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNORMAL)
        win32gui.SetWindowPos(
            hwnd,
            win32con.HWND_NOTOPMOST,
            x,
            y,
            width,
            height,
            win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        debug_log(f"WIN32 MOVE_OK P{index} fullscreen={fullscreen} hwnd={hwnd} rect={width}x{height}+{x}+{y}")
    except Exception as exc:
        debug_log(f"WIN32 MOVE_FAIL P{index} fullscreen={fullscreen} hwnd={hwnd} error={exc}")


def window_geometry(index, count, monitors):
    monitor = monitors[index % len(monitors)]
    columns = min(count, 4)
    rows = (count + columns - 1) // columns
    width = max(320, monitor.width // columns)
    height = max(240, monitor.height // rows)
    column = index % columns
    row = index // columns
    x = monitor.x + column * width
    y = monitor.y + row * height
    return f"{width}x{height}+{x}+{y}"


def is_video_file(path):
    return Path(path).suffix.lower() in VIDEO_EXTENSIONS


def first_video_arg():
    for arg in sys.argv[1:]:
        path = Path(arg)
        if path.exists() and path.is_file():
            return path
    return None


class SyncPlayerApp:
    def __init__(self, initial_video=None):
        self.base_dir = app_base_dir()
        debug_log("=== SyncPlayer started ===")
        self.config = load_config(self.base_dir)
        self.controller = SyncController(self.base_dir, self.config)
        self.remote_server = None
        self.remote_client = None
        self.initial_video = initial_video
        self.auto_save_after_id = None
        self.fast_scroll_active = False

        self.font_family = "Microsoft YaHei UI"
        initial_theme = self.config.get("ui", {}).get("theme", "system")
        ctk.set_appearance_mode(self._ctk_theme_name(initial_theme))
        ctk.set_default_color_theme("blue")
        self.root = self._create_root()
        self.theme_mode = StringVar(master=self.root, value=initial_theme)
        self._set_window_icon()
        self.root.title(APP_NAME)
        try:
            self.root.tk.call("tk", "scaling", 1.35)
        except Exception:
            pass
        self.root.geometry("980x820")
        self.root.minsize(820, 560)
        self.root.resizable(True, True)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._build_ui()
        self._configure_remote_sync()
        self._register_drop_target()

        if self.initial_video is not None:
            self.root.after(200, lambda: self.open_video(self.initial_video))

    def _create_root(self):
        root = ctk.CTk()
        if TkinterDnD is not None:
            try:
                TkinterDnD._require(root)
                debug_log("UI DND_REQUIRE_OK")
            except Exception as exc:
                debug_log(f"UI DND_REQUIRE_FAIL error={exc}")
        return root

    def _app_icon_path(self):
        icon_path = resource_path("assets/logo.ico")
        return icon_path if icon_path.exists() else None

    def _app_icon_photo_path(self):
        png_path = resource_path("assets/logo.png")
        return png_path if png_path.exists() else None

    def _set_window_icon(self):
        # iconphoto uses a full-resolution PNG and is much clearer on high-DPI
        # Windows title bars/taskbars than iconbitmap, which often picks a tiny
        # .ico frame and then scales it up.
        photo_path = self._app_icon_photo_path()
        if photo_path is not None:
            try:
                self.window_icon_photo = PhotoImage(file=str(photo_path))
                self.root.iconphoto(True, self.window_icon_photo)
                debug_log(f"UI ICONPHOTO_OK path={photo_path} size={self.window_icon_photo.width()}x{self.window_icon_photo.height()}")
            except Exception as exc:
                debug_log(f"UI ICONPHOTO_FAIL path={photo_path} error={exc}")

        icon_path = self._app_icon_path()
        if icon_path is not None:
            try:
                self.root.iconbitmap(str(icon_path))
                debug_log(f"UI ICONBITMAP_OK path={icon_path}")
            except Exception as exc:
                debug_log(f"UI ICONBITMAP_FAIL path={icon_path} error={exc}")

    def _ctk_theme_name(self, mode):
        return {
            "light": "light",
            "dark": "dark",
            "system": "system",
        }.get(mode, "system")

    def _theme_label(self, mode):
        return {
            "light": "☀",
            "dark": "☾",
            "system": "☀A",
        }.get(mode, "☀A")

    def cycle_theme(self):
        modes = ["system", "light", "dark"]
        current = self.theme_mode.get()
        next_mode = modes[(modes.index(current) + 1) % len(modes)] if current in modes else "system"
        self.theme_mode.set(next_mode)
        self.config.setdefault("ui", {})["theme"] = next_mode
        ctk.set_appearance_mode(self._ctk_theme_name(next_mode))
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text=self._theme_label(next_mode))
        self._set_status(f"外观模式：{ {'system': '跟随系统', 'light': '浅色', 'dark': '深色'}[next_mode] }")

    def show_about(self):
        messagebox.showinfo(
            f"关于 {APP_NAME}",
            f"{APP_NAME} v{APP_VERSION}\n\n"
            "便携式多屏 mpv 同步播放器。",
        )

    def _register_drop_target(self):
        if DND_FILES is None:
            self._set_status("提示：未安装 tkinterdnd2，窗口拖拽不可用；仍可点击按钮选择视频。")
            return
        registered = 0
        for widget in getattr(self, "drop_widgets", []):
            try:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_drop)
                registered += 1
            except Exception as exc:
                debug_log(f"UI DND_REGISTER_WIDGET_FAIL widget={widget} error={exc}")
        if registered:
            debug_log(f"UI DND_REGISTER_OK count={registered}")
            return
        try:
            self.root.drop_target_register(DND_FILES)
            self.root.dnd_bind("<<Drop>>", self.on_drop)
            debug_log("UI DND_REGISTER_ROOT_OK")
        except Exception as exc:
            debug_log(f"UI DND_REGISTER_FAIL error={exc}")
            self._set_status("提示：窗口拖拽初始化失败；仍可点击按钮选择视频。")

    def _font(self, size, weight="normal"):
        return ctk.CTkFont(family=self.font_family, size=size, weight=weight)

    def _button_style(self, size=13):
        return {
            "font": self._font(size, "bold"),
            "text_color": "white",
        }

    def _load_logo_image(self, size=(42, 42)):
        logo_path = resource_path("assets/logo.png")
        if not logo_path.exists():
            return None
        try:
            image = Image.open(logo_path)
            return ctk.CTkImage(light_image=image, dark_image=image, size=size)
        except Exception as exc:
            debug_log(f"UI LOGO_LOAD_FAIL path={logo_path} error={exc}")
            return None

    def _set_status(self, text):
        if hasattr(self, "status_text"):
            self.status_text.set(text)

    def _bind_fast_scroll(self, widget):
        widget.bind("<Enter>", lambda event: self._set_fast_scroll_active(True), add="+")
        widget.bind("<Leave>", lambda event: self._set_fast_scroll_active(False), add="+")
        self.root.bind_all("<MouseWheel>", self._on_fast_mousewheel, add="+")
        self.root.bind_all("<Button-4>", lambda event: self._scroll_content_fraction(-0.14), add="+")
        self.root.bind_all("<Button-5>", lambda event: self._scroll_content_fraction(0.14), add="+")

    def _set_fast_scroll_active(self, active):
        self.fast_scroll_active = bool(active)

    def _on_fast_mousewheel(self, event):
        if not self.fast_scroll_active:
            return
        direction = -1 if event.delta > 0 else 1
        self._scroll_content_fraction(direction * 0.18)
        return "break"

    def _scroll_content_fraction(self, delta):
        canvas = getattr(getattr(self, "content", None), "_parent_canvas", None)
        if canvas is None:
            return
        top, bottom = canvas.yview()
        if bottom - top >= 0.999:
            return
        canvas.yview_moveto(max(0.0, min(1.0, top + delta)))

    def _section_card(self, parent, row, title, switch_var=None, switch_command=None):
        card = ctk.CTkFrame(
            parent,
            corner_radius=16,
            border_width=1,
            border_color=("#b8c4d4", "#344154"),
            fg_color=("#f7f9fc", "#111827"),
        )
        card.grid(row=row, column=0, sticky="ew", padx=16, pady=(0, 18))
        card.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(card, corner_radius=14, fg_color=("#e7edf6", "#1d2938"))
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 12))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text=title, font=self._font(18, "bold"), anchor="w").grid(row=0, column=0, sticky="w", padx=14, pady=10)
        if switch_var is not None:
            command = switch_command or self.auto_save_settings
            ctk.CTkSwitch(header, text="启用", variable=switch_var, command=command, font=self._font(14, "bold")).grid(row=0, column=1, sticky="e", padx=14, pady=10)

        body = ctk.CTkFrame(card, corner_radius=12, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 12))
        body.grid_columnconfigure(1, weight=1)
        return body

    def _configure_remote_sync(self):
        if self.remote_server is not None:
            self.remote_server.stop()
            self.remote_server = None
        if self.remote_client is not None:
            self.remote_client.stop()
            self.remote_client = None
        self.controller.set_event_callback(None)

        remote_config = self.config.get("remote", {})
        mode = remote_config.get("mode", "off") if remote_config.get("enabled", False) else "off"
        if mode == "host":
            self.remote_server = RemoteSyncServer(self.config, self.controller.remote_state)
            try:
                self.remote_server.start()
                self.controller.set_event_callback(self._broadcast_remote_event)
                self._set_status(f"局域网同步：主机监听 {self.config['remote'].get('host', '0.0.0.0')}:{self.config['remote'].get('port', 6090)}")
            except Exception as exc:
                debug_log(f"REMOTE SERVER_START_FAIL error={exc}")
                self.remote_server = None
                self._set_status(f"局域网同步主机启动失败：{exc}")
                return False
        elif mode == "client":
            self.remote_client = RemoteSyncClient(self.config, self._schedule_remote_command, self.controller.remote_state)
            self.remote_client.start()
            self._set_status(f"局域网同步：正在连接 {self.config['remote'].get('connect_to', '')}")
        return True

    def _broadcast_remote_event(self, message, strong_sync=False):
        if self.remote_server is not None:
            self.remote_server.broadcast(message, strong_sync=strong_sync)

    def _schedule_remote_command(self, command):
        self.root.after(0, lambda: self._apply_remote_command(command))

    def _apply_remote_command(self, command):
        try:
            self.controller.config = self.config
            if command.get("type") == "open" and command.get("path"):
                self._set_status(f"远程启动播放器：{Path(command['path']).name}")
            self.controller.apply_remote_command(command)
            if command.get("type") == "open" and command.get("path"):
                self.root.attributes("-topmost", False)
                self._set_status(f"远程播放：{Path(command['path']).name}")
        except Exception as exc:
            debug_log(f"REMOTE APPLY_FAIL command={command} error={exc}")
            self._set_status("远程同步命令执行失败，请查看日志。")

    def _parse_remote_target(self):
        host = self.remote_connect_host.get().strip()
        if not host:
            raise ValueError("请先填写主机地址，例如 192.168.1.100。")
        return host, int(self.remote_connect_port.get())

    def _split_remote_target(self, target):
        if not target:
            return "192.168.1.100", int(self.config.get("remote", {}).get("port", 6090))
        if ":" in target:
            host, port = target.rsplit(":", 1)
            try:
                return host.strip() or "192.168.1.100", int(port)
            except Exception:
                return host.strip() or "192.168.1.100", 6090
        return target.strip() or "192.168.1.100", int(self.config.get("remote", {}).get("port", 6090))

    def confirm_remote_host(self):
        try:
            port = int(self.remote_port.get())
            if port < 1 or port > 65535:
                raise ValueError
            self._set_status(f"已确认监听设置：{self.remote_host.get().strip() or '0.0.0.0'}:{port}")
        except Exception:
            messagebox.showerror(APP_NAME, "端口必须是 1 到 65535 之间的数字，例如 6090。")

    def confirm_remote_connect_to(self):
        try:
            host, port = self._parse_remote_target()
            if not host or port < 1 or port > 65535:
                raise ValueError
            self._set_status(f"已确认主机地址：{host}:{port}")
        except Exception:
            messagebox.showerror(APP_NAME, "主机地址格式不正确，例如地址 192.168.1.100，端口 6090。")

    def test_remote_connection(self):
        try:
            host, port = self._parse_remote_target()
        except Exception as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        self._set_status(f"正在测试连接：{host}:{port}")
        threading.Thread(target=self._test_remote_connection_worker, args=(host, port), daemon=True).start()

    def _test_remote_connection_worker(self, host, port):
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=3.0)
            sock.settimeout(3.0)
            request = {"type": "time_sync", "client_send_time": time.monotonic()}
            sock.sendall(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            buffer = b""
            deadline = time.monotonic() + 3.0
            response_ok = False
            while time.monotonic() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    message = json.loads(line.decode("utf-8", errors="replace"))
                    if message.get("type") == "time_sync_response":
                        response_ok = True
                        break
                if response_ok:
                    break
            if not response_ok:
                raise RuntimeError("端口可连接，但没有收到 SyncPlayer 主机协议响应。")
            self.root.after(0, lambda: self._set_status(f"连接成功：{host}:{port}"))
            self.root.after(0, lambda: messagebox.showinfo(APP_NAME, f"连接成功：{host}:{port}"))
        except Exception as exc:
            error_text = str(exc)
            debug_log(f"REMOTE TEST_FAIL target={host}:{port} error={exc}")
            self.root.after(0, lambda: self._set_status(f"连接失败：{host}:{port}"))
            self.root.after(0, lambda error_text=error_text: messagebox.showerror(APP_NAME, f"连接失败：{host}:{port}\n\n{error_text}"))
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass

    def apply_remote_settings(self):
        try:
            self.apply_ui_to_config(require_confirmations=False)
            save_config(self.base_dir, self.config)
            applied = self._configure_remote_sync()
            self._set_remote_controls_state()
            if applied:
                self._set_status("局域网同步设置已保存。")
                messagebox.showinfo(APP_NAME, "局域网同步设置已保存。")
            else:
                messagebox.showerror(APP_NAME, "局域网同步设置已保存，但应用失败。请检查监听地址、端口是否被占用。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"应用失败：\n{exc}")

    def on_remote_enabled_changed(self):
        self._set_remote_controls_state()
        try:
            self.apply_ui_to_config(require_confirmations=False)
            save_config(self.base_dir, self.config)
            self._configure_remote_sync()
            self._set_status("局域网同步设置已自动保存。")
        except Exception as exc:
            debug_log(f"UI REMOTE_TOGGLE_SAVE_FAIL error={exc}")

    def _set_remote_controls_state(self):
        if not hasattr(self, "remote_setting_widgets"):
            return
        enabled = bool(self.remote_enabled.get())
        state = "normal" if enabled else "disabled"
        for widget in self.remote_setting_widgets:
            try:
                widget.configure(state=state)
            except Exception:
                pass
        text_color = ("gray10", "gray90") if enabled else ("gray55", "gray45")
        for widget in getattr(self, "remote_setting_labels", []):
            try:
                widget.configure(text_color=text_color)
            except Exception:
                pass
        entry_text_color = ("black", "white") if enabled else ("gray55", "gray45")
        entry_fg_color = ("#f9fafb", "#111827") if enabled else ("#e5e7eb", "#20242c")
        for widget in getattr(self, "remote_setting_entries", []):
            try:
                widget.configure(text_color=entry_text_color, fg_color=entry_fg_color)
            except Exception:
                pass
        button_text_color = "white" if enabled else ("gray70", "gray55")
        for widget in getattr(self, "remote_setting_buttons", []):
            try:
                widget.configure(text_color=button_text_color)
            except Exception:
                pass

    def auto_save_settings(self):
        if self.auto_save_after_id is not None:
            self.root.after_cancel(self.auto_save_after_id)
        self.auto_save_after_id = self.root.after(250, self._flush_auto_save_settings)

    def _flush_auto_save_settings(self):
        self.auto_save_after_id = None
        try:
            self.apply_ui_to_config(require_confirmations=False)
            save_config(self.base_dir, self.config)
            self._set_status("设置已自动保存。")
        except Exception as exc:
            debug_log(f"UI AUTO_SAVE_FAIL error={exc}")

    def _bind_auto_save_entry(self, widget):
        widget.bind("<FocusOut>", lambda event: self.auto_save_settings(), add="+")
        widget.bind("<Return>", lambda event: self.auto_save_settings(), add="+")

    def _build_ui(self):
        self.drop_widgets = []
        self.display_mode = StringVar(value=self.config["display"].get("mode", "auto"))
        self.manual_count = IntVar(value=int(self.config["display"].get("manual_count", 2)))
        self.fullscreen = BooleanVar(value=bool(self.config["display"].get("fullscreen", True)))
        self.local_sync = BooleanVar(value=bool(self.config["display"].get("local_sync", True)))
        self.resume_mode = StringVar(value=self.config["resume"].get("mode", "start_over"))
        self.mute_followers = BooleanVar(value=bool(self.config["mpv"].get("mute_followers", True)))
        self.disable_subtitles = BooleanVar(value=bool(self.config["mpv"].get("disable_subtitles", False)))
        self.hardware_decoding = BooleanVar(value=bool(self.config["mpv"].get("hardware_decoding", True)))
        self.remote_enabled = BooleanVar(value=bool(self.config.get("remote", {}).get("enabled", False)))
        self.remote_mode = StringVar(value=self.config.get("remote", {}).get("mode", "host"))
        self.remote_host = StringVar(value=self.config.get("remote", {}).get("host", "0.0.0.0"))
        self.remote_port = IntVar(value=int(self.config.get("remote", {}).get("port", 6090)))
        connect_host, connect_port = self._split_remote_target(self.config.get("remote", {}).get("connect_to", "192.168.1.100:6090"))
        self.remote_connect_host = StringVar(value=connect_host)
        self.remote_connect_port = IntVar(value=connect_port)
        self.remote_setting_widgets = []
        self.remote_setting_labels = []
        self.remote_setting_entries = []
        self.remote_setting_buttons = []
        self.status_text = StringVar(value="等待视频：拖入视频文件，或点击按钮选择视频。")

        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self.root, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 8))
        header.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            header,
            text=APP_NAME,
            font=self._font(26, "bold"),
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        self.theme_button = ctk.CTkButton(
            header,
            text=self._theme_label(self.theme_mode.get()),
            width=46,
            height=34,
            command=self.cycle_theme,
            **self._button_style(15),
            corner_radius=10,
        )
        self.theme_button.grid(row=0, column=2, sticky="e", padx=(8, 8))
        ctk.CTkButton(
            header,
            text="关于",
            width=64,
            height=34,
            command=self.show_about,
            **self._button_style(13),
            corner_radius=10,
        ).grid(row=0, column=3, sticky="e")

        self.content = ctk.CTkScrollableFrame(self.root, corner_radius=12)
        self.content.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 14))
        self.content.grid_columnconfigure(0, weight=1)
        self._bind_fast_scroll(self.content)
        content = ctk.CTkFrame(self.content, fg_color="transparent")
        content.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        content.grid_columnconfigure(0, weight=1)

        drop_card = ctk.CTkFrame(content, corner_radius=12, fg_color=("#e8edf5", "#1d2938"))
        drop_card.grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 12))
        drop_card.grid_columnconfigure(0, weight=1)
        self.drop_widgets.append(drop_card)

        drop_label = ctk.CTkLabel(
            drop_card,
            text="拖入视频到这里",
            font=self._font(21, "bold"),
        )
        drop_label.grid(row=0, column=0, pady=(20, 12))
        self.drop_widgets.append(drop_label)
        open_button = ctk.CTkButton(
            drop_card,
            text="选择视频并播放",
            command=self.choose_file,
            height=46,
            **self._button_style(15),
            corner_radius=10,
        )
        open_button.grid(row=1, column=0, sticky="ew", padx=72, pady=(0, 22))
        self.drop_widgets.append(open_button)

        local_card = self._section_card(content, 1, "本机多屏同步", self.local_sync)
        ctk.CTkRadioButton(local_card, text="自动检测当前屏幕数量", variable=self.display_mode, value="auto", command=self.auto_save_settings, font=self._font(14)).grid(row=0, column=0, columnspan=3, sticky="w", padx=16, pady=6)
        ctk.CTkRadioButton(local_card, text="手动指定数量", variable=self.display_mode, value="manual", command=self.auto_save_settings, font=self._font(14)).grid(row=1, column=0, sticky="w", padx=16, pady=(6, 16))
        manual_count_entry = ctk.CTkEntry(local_card, textvariable=self.manual_count, width=80, font=self._font(14), justify="center")
        manual_count_entry.grid(row=1, column=1, sticky="w", padx=(0, 10), pady=(6, 16))
        self._bind_auto_save_entry(manual_count_entry)
        ctk.CTkButton(local_card, text="检测屏幕", command=self.show_screen_count, width=96, height=32, **self._button_style(13)).grid(row=1, column=2, sticky="e", padx=(0, 16), pady=(6, 16))

        remote_card = self._section_card(content, 2, "局域网多屏同步", self.remote_enabled, self.on_remote_enabled_changed)
        remote_card.grid_columnconfigure(3, weight=1)
        remote_host_radio = ctk.CTkRadioButton(remote_card, text="作为主机", variable=self.remote_mode, value="host", command=self.auto_save_settings, font=self._font(14))
        remote_host_radio.grid(row=0, column=0, sticky="w", padx=16, pady=6)
        remote_client_radio = ctk.CTkRadioButton(remote_card, text="作为从机", variable=self.remote_mode, value="client", command=self.auto_save_settings, font=self._font(14))
        remote_client_radio.grid(row=0, column=1, sticky="w", padx=8, pady=6)
        self.remote_setting_widgets.extend([remote_host_radio, remote_client_radio])
        remote_host_label = ctk.CTkLabel(remote_card, text="主机监听地址", font=self._font(13), anchor="w")
        remote_host_label.grid(row=2, column=0, sticky="w", padx=16, pady=(12, 4))
        remote_port_label = ctk.CTkLabel(remote_card, text="端口", font=self._font(13), anchor="w")
        remote_port_label.grid(row=2, column=1, sticky="w", padx=8, pady=(12, 4))
        remote_host_entry = ctk.CTkEntry(remote_card, textvariable=self.remote_host, width=170, font=self._font(13))
        remote_host_entry.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        self._bind_auto_save_entry(remote_host_entry)
        remote_port_entry = ctk.CTkEntry(remote_card, textvariable=self.remote_port, width=92, font=self._font(13), justify="center")
        remote_port_entry.grid(row=3, column=1, sticky="w", padx=8, pady=(0, 10))
        self._bind_auto_save_entry(remote_port_entry)
        self.remote_setting_widgets.extend([remote_host_entry, remote_port_entry])
        self.remote_setting_entries.extend([remote_host_entry, remote_port_entry])
        remote_connect_host_label = ctk.CTkLabel(remote_card, text="从机连接主机地址", font=self._font(13), anchor="w")
        remote_connect_host_label.grid(row=4, column=0, sticky="w", padx=16, pady=(8, 4))
        remote_connect_port_label = ctk.CTkLabel(remote_card, text="端口", font=self._font(13), anchor="w")
        remote_connect_port_label.grid(row=4, column=1, sticky="w", padx=8, pady=(8, 4))
        remote_connect_host_entry = ctk.CTkEntry(remote_card, textvariable=self.remote_connect_host, width=170, font=self._font(13))
        remote_connect_host_entry.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 14))
        self._bind_auto_save_entry(remote_connect_host_entry)
        remote_connect_port_entry = ctk.CTkEntry(remote_card, textvariable=self.remote_connect_port, width=92, font=self._font(13), justify="center")
        remote_connect_port_entry.grid(row=5, column=1, sticky="w", padx=8, pady=(0, 14))
        self._bind_auto_save_entry(remote_connect_port_entry)
        test_button = ctk.CTkButton(remote_card, text="测试连接", command=self.test_remote_connection, width=100, height=32, **self._button_style(13))
        test_button.grid(row=5, column=2, sticky="w", padx=8, pady=(0, 14))
        save_remote_button = ctk.CTkButton(remote_card, text="保存局域网设置", command=self.apply_remote_settings, height=36, **self._button_style(13))
        save_remote_button.grid(row=6, column=0, columnspan=4, sticky="ew", padx=16, pady=(0, 16))
        self.remote_setting_widgets.extend([remote_connect_host_entry, remote_connect_port_entry, test_button, save_remote_button])
        self.remote_setting_entries.extend([remote_connect_host_entry, remote_connect_port_entry])
        self.remote_setting_buttons.extend([test_button, save_remote_button])
        self.remote_setting_labels.extend([remote_host_label, remote_port_label, remote_connect_host_label, remote_connect_port_label])
        self._set_remote_controls_state()

        playback_card = self._section_card(content, 3, "播放设置")
        ctk.CTkCheckBox(playback_card, text="默认全屏", variable=self.fullscreen, command=self.auto_save_settings, font=self._font(14)).grid(row=0, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkCheckBox(playback_card, text="第 2 个及之后默认静音", variable=self.mute_followers, command=self.auto_save_settings, font=self._font(14)).grid(row=1, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkCheckBox(playback_card, text="默认关闭字幕", variable=self.disable_subtitles, command=self.auto_save_settings, font=self._font(14)).grid(row=2, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkCheckBox(playback_card, text="启用硬件解码", variable=self.hardware_decoding, command=self.auto_save_settings, font=self._font(14)).grid(row=3, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkLabel(playback_card, text="播放进度", font=self._font(15, "bold"), anchor="w").grid(row=4, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkRadioButton(playback_card, text="每次从头开始", variable=self.resume_mode, value="start_over", command=self.auto_save_settings, font=self._font(14)).grid(row=5, column=0, sticky="w", padx=16, pady=6)
        ctk.CTkRadioButton(playback_card, text="记住上次播放进度", variable=self.resume_mode, value="remember", command=self.auto_save_settings, font=self._font(14)).grid(row=6, column=0, sticky="w", padx=16, pady=(6, 16))

        status_bar = ctk.CTkFrame(content, corner_radius=10, fg_color=("#dfe6ef", "#151c26"))
        status_bar.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
        status_bar.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            status_bar,
            textvariable=self.status_text,
            font=self._font(13),
            text_color=("gray25", "gray75"),
            anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=16, pady=10)

    def run(self):
        self.root.mainloop()

    def choose_file(self):
        path = filedialog.askopenfilename(
            title="选择要对比播放的视频",
            filetypes=[("视频文件", "*.mkv *.mp4 *.mov *.m2ts *.mts *.ts *.avi *.webm *.wmv *.flv"), ("所有文件", "*.*")],
        )
        if path:
            self.open_video(path)

    def on_drop(self, event):
        paths = self.root.tk.splitlist(event.data)
        for path in paths:
            if Path(path).is_file():
                self.open_video(path)
                return

    def open_video(self, path):
        try:
            self.apply_ui_to_config()
        except ValueError as exc:
            messagebox.showerror(APP_NAME, str(exc))
            return
        video_path = Path(path)
        if not video_path.exists():
            messagebox.showerror(APP_NAME, f"文件不存在：\n{video_path}")
            return
        if not is_video_file(video_path):
            if not messagebox.askyesno(APP_NAME, "这个文件扩展名不像视频，仍要尝试打开吗？"):
                return

        try:
            self.controller.config = self.config
            self._set_status(f"正在启动播放器：{video_path.name}")
            self.root.update_idletasks()
            self.controller.start(video_path)
            self.root.attributes("-topmost", False)
            self._set_status(f"已启动播放：{video_path.name}")
        except Exception as exc:
            self._set_status("启动失败，请查看错误信息。")
            messagebox.showerror(APP_NAME, str(exc))

    def apply_ui_to_config(self, require_confirmations=True):
        try:
            manual_count = int(self.manual_count.get())
        except Exception as exc:
            raise ValueError("手动屏幕数量必须是数字。") from exc
        if manual_count < 1:
            raise ValueError("手动屏幕数量必须至少为 1。")
        if manual_count > 16:
            raise ValueError("手动屏幕数量不能超过 16。")
        try:
            remote_port = int(self.remote_port.get())
        except Exception as exc:
            raise ValueError("远程同步端口必须是数字。") from exc
        if remote_port < 1 or remote_port > 65535:
            raise ValueError("远程同步端口必须在 1 到 65535 之间。")
        try:
            remote_connect_port = int(self.remote_connect_port.get())
        except Exception as exc:
            raise ValueError("从机连接端口必须是数字。") from exc
        if remote_connect_port < 1 or remote_connect_port > 65535:
            raise ValueError("从机连接端口必须在 1 到 65535 之间。")
        remote_mode = self.remote_mode.get()
        if remote_mode not in {"off", "host", "client"}:
            remote_mode = "host"
        remote_enabled = bool(self.remote_enabled.get())
        remote_host = self.remote_host.get().strip() or "0.0.0.0"
        remote_connect_host = self.remote_connect_host.get().strip()
        remote_connect_to = f"{remote_connect_host}:{remote_connect_port}"
        if remote_enabled and remote_mode == "off":
            raise ValueError("启用局域网同步后，请选择作为主机或作为从机。")
        if remote_enabled and remote_mode == "client" and not remote_connect_host:
            raise ValueError("作为从机时必须填写主机地址，例如 192.168.1.100。")
        self.config["display"]["mode"] = self.display_mode.get()
        self.config["display"]["manual_count"] = manual_count
        self.config["display"]["fullscreen"] = bool(self.fullscreen.get())
        self.config["display"]["local_sync"] = bool(self.local_sync.get())
        self.config["resume"]["mode"] = self.resume_mode.get()
        self.config["mpv"]["mute_followers"] = bool(self.mute_followers.get())
        self.config["mpv"]["disable_subtitles"] = bool(self.disable_subtitles.get())
        self.config["mpv"]["hardware_decoding"] = bool(self.hardware_decoding.get())
        self.config.setdefault("remote", {})["enabled"] = remote_enabled
        self.config.setdefault("remote", {})["mode"] = remote_mode
        self.config["remote"]["host"] = remote_host
        self.config["remote"]["port"] = remote_port
        self.config["remote"]["connect_to"] = remote_connect_to
        self.config.setdefault("ui", {})["theme"] = self.theme_mode.get()

    def save_settings(self):
        try:
            self.apply_ui_to_config()
            save_config(self.base_dir, self.config)
            self._configure_remote_sync()
            self._set_status("设置已保存。")
            messagebox.showinfo(APP_NAME, "设置已保存。")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"保存失败：\n{exc}")

    def show_screen_count(self):
        monitors = get_display_layout()
        details = "\n".join(
            f"屏幕 {index + 1}: {m.name or '未知显示器'} {m.width}x{m.height}+{m.x}+{m.y}"
            for index, m in enumerate(monitors)
        )
        self._set_status(f"检测到 {len(monitors)} 个屏幕。")
        messagebox.showinfo(APP_NAME, f"检测到 {len(monitors)} 个屏幕。\n\n{details}")

    def on_close(self):
        if self.remote_server is not None:
            self.remote_server.stop()
        if self.remote_client is not None:
            self.remote_client.stop()
        self.controller.stop()
        self.root.destroy()


if __name__ == "__main__":
    setup_dpi_awareness()
    SyncPlayerApp(first_video_arg()).run()
