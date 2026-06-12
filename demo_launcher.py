import flet as ft
import subprocess
import threading
import os
import signal
import socket
import time
import urllib.request
import urllib.error
from pathlib import Path

HOME = Path.home()

# 各デモのメタデータ。
#   dir          : start_all.sh / stop_all.sh のあるディレクトリ
#   ports        : このデモが bind する Web ポート（停止後の解放待ちに使う）
#   ready_url    : 起動完了の判定に叩く URL（応答すれば ready）
#   ready_timeout: ready 待ちの最大秒数（モデルロード時間を見込む）
DEMOS = {
    "AIassistant": {
        "dir": HOME / "AIassistant",
        "ports": [8000, 8001, 8080],
        "ready_url": "http://localhost:8000/status",
        "ready_timeout": 600,
    },
    "LLaVA": {
        "dir": HOME / "LLaVA",
        "ports": [8080, 8081],
        "ready_url": "http://localhost:8080/",
        "ready_timeout": 120,
    },
    "RealtimeDepth": {
        "dir": HOME / "RealtimeDepth",
        "ports": [8000],
        "ready_url": "http://localhost:8000/",
        "ready_timeout": 180,
    },
}

# 走行中の start_all.sh を name -> Popen で覚えておく（BUG-3: ゾンビ起動スクリプト対策）。
_running_starts: dict[str, subprocess.Popen] = {}
_starts_lock = threading.Lock()

# ---- low-level helpers ----

def _http_ok(url: str, timeout: float = 2.0) -> bool:
    """url が何らかの HTTP 応答を返せば True（4xx/5xx でもサーバは生きている）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status < 600
    except urllib.error.HTTPError:
        return True
    except Exception:
        return False

def _port_free(port: int) -> bool:
    """127.0.0.1:port で listen しているものが無ければ True。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) != 0

def _wait_ports_free(name: str, timeout: float = 30.0) -> bool:
    """name のデモが使うポートがすべて解放されるまで待つ（BUG-2: ポート競合対策）。"""
    ports = DEMOS[name].get("ports", [])
    if not ports:
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        if all(_port_free(p) for p in ports):
            return True
        time.sleep(1)
    return all(_port_free(p) for p in ports)

def _kill_start_process(name: str):
    """name に紐づく走行中の start_all.sh をプロセスグループごと止める（BUG-3）。"""
    with _starts_lock:
        proc = _running_starts.pop(name, None)
    if proc is None or proc.poll() is not None:
        return
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return
    # 数秒待って残っていれば SIGKILL。
    for _ in range(10):
        if proc.poll() is not None:
            return
        time.sleep(0.3)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass

# ---- demo control ----

def stop_demo(name: str):
    """1 デモを停止。失敗時は例外を送出する（BUG-7: 失敗を握り潰さない）。"""
    _kill_start_process(name)
    directory = DEMOS[name]["dir"]
    script = directory / "stop_all.sh"
    if script.exists():
        # timeout を 120s に延長（BUG-8: llama-server のアンロード等が 60s を超える）。
        subprocess.run(
            ["bash", "stop_all.sh"],
            cwd=str(directory),
            timeout=120,
            check=True,
        )

def start_demo(name: str) -> subprocess.Popen:
    """他デモも対象デモも一旦止めてから起動する（BUG-1: 同一デモ再起動 / BUG-2 / BUG-3）。"""
    # 対象自身も含め全デモを停止（BUG-1: 同じボタン2回押しで already running 失敗するのを防ぐ）。
    for demo_name in DEMOS:
        try:
            stop_demo(demo_name)
        except Exception:
            # これから起動し直すので停止の失敗はベストエフォートで無視。
            pass

    # ポート解放を待ってから起動（BUG-2: bind 失敗 → ready タイムアウト → Chrome 開かずを防ぐ）。
    _wait_ports_free(name)

    directory = DEMOS[name]["dir"]
    script = directory / "start_all.sh"
    if not script.exists():
        raise FileNotFoundError(f"start_all.sh が見つかりません: {script}")

    # start_new_session=True で独立したプロセスグループにし、後で killpg できるようにする（BUG-3）。
    proc = subprocess.Popen(
        ["bash", "start_all.sh"],
        cwd=str(directory),
        start_new_session=True,
    )
    with _starts_lock:
        _running_starts[name] = proc
    return proc

def wait_ready(name: str, proc: subprocess.Popen):
    """start_all.sh の完了/失敗を ready_url のポーリングで判定する（BUG-6）。

    戻り値: (成功か, エラーメッセージ or None)
    """
    info = DEMOS[name]
    url = info.get("ready_url")
    timeout = info.get("ready_timeout", 120)
    deadline = time.time() + timeout
    while time.time() < deadline:
        rc = proc.poll()
        if rc is not None and rc != 0:
            return False, f"起動スクリプトが終了コード {rc} で終了しました"
        if url and _http_ok(url):
            return True, None
        if not url and rc == 0:
            return True, None
        time.sleep(2)
    return False, "タイムアウト（サービスが応答しませんでした）"

def stop_all_demos():
    """全デモを停止。1 つが失敗しても残りを止め、エラー一覧を返す（BUG-8）。"""
    errors = []
    for name in DEMOS:
        try:
            stop_demo(name)
        except subprocess.TimeoutExpired:
            errors.append(f"{name}: 停止がタイムアウトしました")
        except subprocess.CalledProcessError as ex:
            errors.append(f"{name}: 停止スクリプトが失敗 (code {ex.returncode})")
        except Exception as ex:
            errors.append(f"{name}: {ex}")
    return errors

# ---- UI ----

def main(page: ft.Page):
    page.title = "Demo Launcher"
    page.theme_mode = ft.ThemeMode.DARK
    page.bgcolor = "#1a1a2e"
    page.window.width = 460
    page.window.height = 580
    page.padding = 30

    status = ft.Text(
        "待機中",
        size=13,
        color="#888888",
        text_align=ft.TextAlign.CENTER,
    )
    progress = ft.ProgressBar(visible=False, width=380, color="#4fc3f7", bgcolor="#333355")

    action_buttons: list[ft.FilledButton] = []

    def set_status(msg: str, color="#888888", busy=False):
        status.value = msg
        status.color = color
        progress.visible = busy
        # busy 中はボタンを無効化して start/stop の同時実行を防ぐ（BUG-9）。
        for b in action_buttons:
            b.disabled = busy
        page.update()

    def run_bg(task):
        """flet 管理スレッドで UI 更新が反映されるよう page.run_thread を使う（BUG-5）。"""
        runner = getattr(page, "run_thread", None)
        if callable(runner):
            runner(task)
        else:
            threading.Thread(target=task, daemon=True).start()

    def make_start_handler(name: str):
        def handler(e):
            set_status(f"{name} を起動中 (他デモを停止してから起動)...", "#ffd54f", busy=True)

            def task():
                try:
                    proc = start_demo(name)
                    set_status(f"{name} のサービス起動を待機中...", "#ffd54f", busy=True)
                    ok, err = wait_ready(name, proc)
                    if ok:
                        set_status(f"{name} を起動しました", "#81c784")
                    else:
                        set_status(f"{name} の起動に失敗: {err}", "#ef5350")
                except Exception as ex:
                    set_status(f"エラー: {ex}", "#ef5350")

            run_bg(task)

        return handler

    def handle_stop_all(e):
        set_status("全デモを停止中...", "#ffb74d", busy=True)

        def task():
            try:
                errors = stop_all_demos()
                if errors:
                    set_status("一部停止に失敗: " + "; ".join(errors), "#ef5350")
                else:
                    set_status("全デモを停止しました", "#ffb74d")
            except Exception as ex:
                set_status(f"エラー: {ex}", "#ef5350")

        run_bg(task)

    def handle_shutdown(e):
        def confirm(ev):
            page.pop_dialog()
            set_status("シャットダウン準備中（デモを停止）...", "#ef5350", busy=True)

            def task():
                # シャットダウン前に全デモを停止（ベストエフォート）。
                stop_all_demos()
                try:
                    # -n: パスワードを要求されたら待たずに即失敗させる（GUI には入力手段が無い）。
                    result = subprocess.run(
                        ["sudo", "-n", "shutdown", "-h", "now"],
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    if result.returncode != 0:
                        msg = result.stderr.strip() or f"終了コード {result.returncode}"
                        set_status(
                            f"シャットダウン失敗（sudoers 未設定の可能性: setup_sudoers.sh を実行）: {msg}",
                            "#ef5350",
                        )
                except Exception as ex:
                    set_status(f"シャットダウン失敗: {ex}", "#ef5350")

            run_bg(task)

        def cancel(ev):
            page.pop_dialog()

        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text("シャットダウン確認", color="#ef5350"),
            content=ft.Text("PCをシャットダウンしますか？\nこの操作は元に戻せません。"),
            actions=[
                ft.TextButton(
                    "シャットダウン",
                    on_click=confirm,
                    style=ft.ButtonStyle(color="#ef5350"),
                ),
                ft.TextButton("キャンセル", on_click=cancel),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        page.show_dialog(dlg)

    def btn(label: str, handler, bg: str, icon_name: str):
        button = ft.FilledButton(
            content=ft.Row(
                [
                    ft.Icon(icon_name, size=20, color="white"),
                    ft.Text(label, size=15, weight=ft.FontWeight.W_500, color="white"),
                ],
                alignment=ft.MainAxisAlignment.CENTER,
                spacing=10,
                tight=True,
            ),
            on_click=handler,
            width=380,
            height=54,
            style=ft.ButtonStyle(
                bgcolor={
                    ft.ControlState.DEFAULT: bg,
                    ft.ControlState.HOVERED:  bg,
                    ft.ControlState.PRESSED:  bg,
                },
                shape=ft.RoundedRectangleBorder(radius=10),
                elevation={"default": 3, "hovered": 6},
            ),
        )
        action_buttons.append(button)
        return button

    page.add(
        ft.Column(
            [
                ft.Text("Demo Launcher", size=26, weight=ft.FontWeight.BOLD, color="white"),
                ft.Text("デモ起動管理ツール", size=12, color="#666688"),
                ft.Divider(color="#333355", height=24),

                btn("AIassistant を起動",       make_start_handler("AIassistant"),      "#2e7d32", ft.Icons.PLAY_ARROW_ROUNDED),
                btn("LLaVA を起動",         make_start_handler("LLaVA"),        "#1565c0", ft.Icons.PLAY_ARROW_ROUNDED),
                btn("RealtimeDepth を起動", make_start_handler("RealtimeDepth"), "#6a1b9a", ft.Icons.PLAY_ARROW_ROUNDED),

                ft.Divider(color="#333355", height=24),

                btn("全て停止",    handle_stop_all, "#e65100", ft.Icons.STOP_ROUNDED),
                btn("PC 電源オフ", handle_shutdown,  "#b71c1c", ft.Icons.POWER_SETTINGS_NEW_ROUNDED),

                ft.Divider(color="#333355", height=14),
                progress,
                status,
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=12,
        )
    )

    # DISPLAY が無い環境では子プロセスの Chrome が黙って失敗するため警告する（BUG-4）。
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        set_status("警告: DISPLAY が無いため Chrome が開けない可能性があります", "#ef5350")

ft.run(main)
