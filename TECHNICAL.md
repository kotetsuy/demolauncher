# TECHNICAL.md — Demo Launcher internals

Subject: `demo_launcher.py` (single file, 391 lines)
For the user-facing guide see [`README.md`](./README.md); for how this design
came about see [`BUG.md`](./BUG.md).
日本語版は [`TECHNICALJ.md`](./TECHNICALJ.md)。

---

## 1. Structure

One file, no local modules, in four layers top to bottom.

| Layer | Lines | Contents |
|---|---|---|
| Single-instance guard | 16–31 | Multiple-launch prevention via an abstract UNIX socket |
| Configuration | 38–72 | The `DEMOS` dict — the only place to add or change a demo |
| Low-level helpers | 80–128 | HTTP liveness, port-release waiting, process-group kill |
| Demo control | 134–209 | `stop_demo` / `start_demo` / `wait_ready` / `stop_all_demos` |
| UI | 212–383 | The flet GUI and its event handlers |

Nothing here touches what the demos are made of (llama-server, tmux, Chrome,
ROCm). The launcher knows exactly three things: **that `start_all.sh` and
`stop_all.sh` exist**, **which ports the demo binds**, and **which URL says it
is ready**. That is the entire contract, which is why ROCm upgrades and
demo-side rewrites do not propagate into this tool.

---

## 2. The `DEMOS` schema

```python
"AI2048": {
    "dir":           HOME / "AI2048",                 # where start_all.sh / stop_all.sh live
    "ports":         [8000, 8009, 8080, 9222],        # ports to wait on after stopping
    "ready_url":     "http://localhost:8000/status",  # polled to detect a completed start
    "ready_timeout": 600,                             # how long to wait for ready, in seconds
},
```

- **`ports`** must be the *complete* set of ports the demo binds. Miss one and
  `_wait_ports_free()` returns early, letting the next demo start into a bind
  conflict — exactly what happened with LLaVA-NPU's NPU sidecar on `:8082`
- **`ready_url`** should point at whatever comes up *last*. AIassistant,
  EarthTourGuide and AI2048 all use `:8000/status` because that endpoint only
  starts responding once llama has finished loading
- **`ready_timeout`** is dictated by llama-server's model load time. LLaVA-NPU
  gets away with 120 s because its `ready_url` is the web server (`:8080/`),
  which does not wait for llama

### Port contention matrix

| Port | AIassistant | LLaVA-NPU | RealtimeDepth | EarthTourGuide | AI2048 |
|---|---|---|---|---|---|
| 8000 | ● | | ● | ● | ● |
| 8001 | ● | | | ● | |
| 8002–8003 | | | | ● | |
| 8009 | | | | | ● |
| 8080 | ● | ● | | ● | ● |
| 8081–8082 | | ● | | | |
| 9222 | | | | | ● |

Four demos contend for `:8000` and four for `:8080`. **Mutually exclusive
startup is a hard design requirement** — "run several demos at once" is not an
option that exists.

---

## 3. Start sequence (`start_demo` → `wait_ready`)

```
button pressed
  ├─ set_status(busy=True)            disable every button
  └─ run_bg(task)                     dispatch through page.run_thread
       │
       ├─ stop_demo() for every demo  ← including the target itself (important)
       │    ├─ _kill_start_process()  killpg the previous start_all.sh
       │    └─ bash stop_all.sh       check=True / timeout=120
       │
       ├─ _wait_ports_free(name)      up to 30s for the target's ports
       │
       ├─ Popen(bash start_all.sh, start_new_session=True)
       │
       └─ wait_ready()                poll ready_url every 2s
            ├─ responded         → "started"
            ├─ script exited != 0 → report that exit code
            └─ ready_timeout hit  → "timeout"
```

### Why the target demo is stopped too

Most `start_all.sh` scripts defend themselves with "already running, exit 1"
(an existing tmux session, a live PID file). Starting without stopping the
target makes the second press of the same button fail every time. Stopping
everything first makes the button idempotent: it means "**leave this demo, and
only this demo, running**".

### What `start_new_session=True` buys

`start_all.sh` brings up tmux, llama-server and Chrome, then blocks in
`wait_http` for up to 600 seconds. If another button is pressed during that
wait, `stop_all.sh` kills the individual processes but **not the running
`start_all.sh` itself**. The survivor then recreates tmux sessions or opens
Chrome later, colliding with the new demo.

So it is started in its own process group, its `Popen` is kept in
`_running_starts`, and stopping sends `os.killpg(pgid, SIGTERM)` followed by
`SIGKILL` three seconds later (`_kill_start_process`, lines 108–128).

---

## 4. Liveness rules

### `_http_ok()` — 4xx/5xx count as alive

```python
except urllib.error.HTTPError:
    return True
```

The question is not "is the app behaving correctly" but "**is a server process
listening and speaking HTTP**". Anything that can return a 404 or a 500 has
finished starting. Only refused connections and timeouts count as failure.

### `_port_free()` — connect-based

A non-zero `connect_ex()` means nobody is listening. Because it does not try to
bind, it does not produce false positives for TIME_WAIT sockets awaiting
`SO_REUSEADDR`.

### A `_wait_ports_free()` timeout is not a failure

If 30 seconds pass without the ports being released, it returns the last
verdict and the start proceeds anyway. Aborting here would make the launcher
unusable whenever an unrelated background process happens to hold one of these
ports. A failed bind is caught downstream by `wait_ready` as a timeout.

---

## 5. Single-instance guard

```python
_SINGLE_INSTANCE_ADDR = "\0demo_launcher_single_instance"
```

The leading `\0` selects Linux's **abstract socket namespace**: no filesystem
entry is created and the kernel releases the name when the process exits.

Compared to a lock file:

- Nothing is left behind after a crash or `SIGKILL` — no stale-lock cleanup
- No dependency on `/tmp` permissions or tmpfs cleanup policy

The global reference in `_instance_lock_socket` exists to stop the socket from
being garbage-collected, which would silently release the lock. Do not drop it.

The check runs at import time (line 386); if another instance holds the lock the
process prints a message and leaves via `SystemExit(0)`. It is deliberately not
an error, so that a duplicate autostart invocation does not litter the logs.

---

## 6. UI threading model

flet expects UI updates on its event loop. Calling `page.update()` from a plain
`threading.Thread` may silently drop the update or raise — timing dependent.
Worse, the `except` branch calls the same `set_status()`, so it fails a second
time and the daemon thread dies quietly, freezing the UI on "starting…".

```python
def run_bg(task):
    runner = getattr(page, "run_thread", None)
    if callable(runner):
        runner(task)
    else:
        threading.Thread(target=task, daemon=True).start()
```

The `getattr` probe is a fallback so that older flet versions without
`page.run_thread` can still launch. flet 0.86 has it, so the first branch is
what normally runs.

`set_status(busy=True)` disables every entry in `action_buttons`, because a
start thread running alongside a stop thread would target ports that are still
being torn down.

---

## 7. Power-off

```python
subprocess.run(["sudo", "-n", "shutdown", "-h", "now"], capture_output=True, timeout=15)
```

`-n` (non-interactive) is the point. Without sudoers configured, sudo asks for a
password, and with no way to type one the call would simply hang. With `-n` it
fails immediately and non-zero, so stderr can be shown in the status line.

`stop_all_demos()` runs first on a best-effort basis, to give llama-server a
chance to unload its model cleanly.

---

## 8. Adding a demo

1. Add one entry to `DEMOS` (`dir` / `ports` / `ready_url` / `ready_timeout`)
2. Add one `btn(...)` line to `page.add(...)` in `main()`

The reliable way to enumerate `ports` is to start the demo and look:

```bash
cd ~/<demo> && bash start_all.sh
ss -ltnp | grep <process>
```

Where a port is configurable (LLaVA-NPU's `npu.port` in `config.yaml`, for
example), check the configured value, not just the default.

---

## 9. Known limitations

- **No live demo state** — the status line reports the outcome of the last
  operation; it does not poll whether a demo is currently up. Restarting the
  launcher resets it to "待機中", even if a demo is running
- **Stop failures are swallowed during a start** — the stop loop inside
  `start_demo` is best-effort, since everything is about to be restarted
  anyway. Use the "全て停止" button when you want to see stop failures
- **`icon.svg` is unused** — an assets directory is handed to flet, but nothing
  in the code references it
