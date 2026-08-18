"""Exercise the hardened tools through their real code paths.

Run directly: .venv/bin/python tests/test_tools.py
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aios_core.agent.tools import filesystem, processes, search, shell  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="aios-tool-test-"))
PASS = 0
FAIL = []


def check(name, condition, detail=""):
    global PASS
    if condition:
        PASS += 1
        print(f"  ok: {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL: {name} {detail}")


def run_bash(command, timeout=None):
    return asyncio.run(shell._run_bash(command, timeout, cwd=str(TMP)))


def remove_full_output_log(output):
    match = re.search(r"full output: ([^)\n]+)", output)
    if match:
        Path(match.group(1)).unlink(missing_ok=True)


print("== read ==")
target = TMP / "sample.py"
target.write_text("\n".join(f"line {i}" for i in range(1, 51)) + "\n")

out = filesystem.read(str(target))
check("read basic", "   1| line 1" in out and "  50| line 50" in out, out[:120])

out = filesystem.read(str(target), offset=10, limit=5)
check("read pagination window", "  11| line 11" in out and "  15| line 15" in out and "line 16" not in out)
check("read continue hint", "continue with offset=15" in out, out[-90:])

out = filesystem.read(str(TMP / "sampel.py"))
check("read not-found suggestion", "did you mean" in out and "sample.py" in out, out)

out = filesystem.read(str(TMP))
check("read directory listing hint", "is a directory" in out and "sample.py" in out, out)

binary = TMP / "blob.dat"
binary.write_bytes(b"\x00\x01\x02real\x00stuff" * 20)
out = filesystem.read(str(binary))
check("read binary blocked", "error" in out and "binary" in out.lower(), out)

out = filesystem.read(str(TMP / "pic.png"))
check("read image error", "image file" in out or "not found" in out, out)

out = filesystem.read("/dev/urandom")
check("read device blocked", "device" in out, out)

longline = TMP / "long.txt"
longline.write_text("x" * 10_000 + "\n")
out = filesystem.read(str(longline))
check("read per-line truncation", "[line truncated]" in out and len(out) < 5_000, f"len={len(out)}")

out = filesystem.read(str(target), offset=500)
check("read offset beyond EOF", "beyond end of file" in out, out)

print("== read: large-file streaming ==")
big = TMP / "big.log"
with open(big, "w") as f:
    for i in range(200_000):
        f.write(f"entry number {i}\n")
start = time.time()
out = filesystem.read(str(big), offset=150_000, limit=3)
elapsed = time.time() - start
check("large read exact window", "150001| entry number 150000" in out and "entry number 150003" not in out, out[:200])
check("large read total count", "of 200000" in out, out[-120:])
check("large read reasonable time", elapsed < 10, f"{elapsed:.1f}s")

print("== repeat-call loop guard ==")
loop_target = TMP / "loop.txt"
loop_target.write_text("stable\n")
results = [filesystem.read(str(loop_target)) for _ in range(4)]
check("repeat warn at 3", "repeated 3 times" in results[2], results[2][-120:])
check("repeat block at 4", results[3].startswith("BLOCKED"), results[3][:80])

print("== write ==")
dest = TMP / "new" / "file.txt"
out = filesystem.write(str(dest), "hello\nworld\n")
check("write creates dirs + reports", out.startswith("ok:") and "2 lines" in out, out)
check("write content correct", dest.read_text() == "hello\nworld\n")

out = filesystem.write(str(dest), " 12| numbered\n 13| lines\n 14| here\n")
check("write refuses line-numbered content", "read-tool output" in out, out)

unseen = TMP / "unseen.txt"
unseen.write_text("original")
out = filesystem.write(str(unseen), "overwritten")
check("write blind-overwrite warning", "was not read" in out, out)

stale = TMP / "stale.txt"
stale.write_text("v1\n")
filesystem.read(str(stale))
time.sleep(0.02)
stale.write_text("v2-external\n")
os.utime(stale, (time.time() + 5, time.time() + 5))
out = filesystem.write(str(stale), "v3\n")
check("write stale-read warning", "modified" in out, out)

out = filesystem.write(str(TMP / "ansi.txt"), "has \x1b[31mcolor\x1b[0m codes")
check("write ANSI warning", "ANSI escape" in out, out)

print("== edit ==")
crlf = TMP / "crlf.txt"
crlf.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")
out = filesystem.edit(str(crlf), "beta\ngamma", "beta\nDELTA")
check("edit CRLF normalization", out.startswith("ok:"), out)
check("edit preserves CRLF", b"DELTA\r\n" in crlf.read_bytes(), crlf.read_bytes())

multi = TMP / "multi.txt"
multi.write_text("dup\ndup\nother\n")
out = filesystem.edit(str(multi), "dup", "DUP")
check("edit uniqueness error", "2 times" in out, out)
out = filesystem.edit(str(multi), "dup", "DUP", all=True)
check("edit all=true", out.startswith("ok: replaced 2"), out)

ws = TMP / "ws.txt"
ws.write_text("def foo():\n    return 1\n")
out = filesystem.edit(str(ws), "def foo():\n  return 1", "def foo():\n  return 2")
check("edit whitespace hint", "whitespace/indentation" in out, out)

out = filesystem.edit(str(TMP / "missing.txt"), "a", "b")
check("edit missing file error", "not found" in out, out)

bom = TMP / "bom.txt"
bom.write_bytes("﻿key=1\n".encode("utf-8"))
out = filesystem.edit(str(bom), "key=1", "key=2")
check("edit BOM file ok", out.startswith("ok:"), out)
check("edit preserves BOM", bom.read_bytes().startswith(b"\xef\xbb\xbf") and b"key=2" in bom.read_bytes(), bom.read_bytes())

print("== glob ==")
(TMP / "node_modules" / "pkg").mkdir(parents=True)
(TMP / "node_modules" / "pkg" / "junk.py").write_text("x")
(TMP / "src").mkdir()
(TMP / "src" / "app.py").write_text("x")
out = search.glob("**/*.py", str(TMP))
check("glob finds files", "app.py" in out and "sample.py" in out, out)
check("glob skips node_modules", "junk.py" not in out, out)
out = search.glob("node_modules/**/*.py", str(TMP))
check("glob explicit noise dir allowed", "junk.py" in out, out)

print("== grep ==")
hay = TMP / "hay"
hay.mkdir()
for i in range(3):
    (hay / f"f{i}.txt").write_text(f"needle {i}\nplain\n" * 2)
(hay / "bin.dat").write_bytes(b"\x00needle\x00")

out = search.grep("needle", str(hay))
check("grep finds matches", "needle 0" in out and "needle 2" in out, out[:200])
check("grep file:line format", ":1:" in out or ":3:" in out, out[:200])

out = search.grep("(unclosed", str(hay))
check("grep invalid regex", out.startswith("error: invalid regex"), out)

out = search.grep("needle", str(hay), limit=2)
check("grep paging notice", "continue with offset=2" in out, out)

out = search.grep("plain", str(hay / "f0.txt"), context=1)
check("grep single file + context", "needle" in out and "plain" in out, out)

out = search.grep("zzz-not-there", str(hay))
check("grep no matches", out == "none", out)

# Force the pure-Python fallback and repeat the essentials.
search._rg_path = None
out = search.grep("needle", str(hay))
check("grep python fallback matches", "needle 1" in out, out[:200])
check("grep python fallback skips binary", "bin.dat" not in out, out)
out = search.grep("plain", str(hay / "f1.txt"), context=1)
check("grep python fallback context", "-needle" in out.replace(str(hay / 'f1.txt'), ''), out)
search._rg_path = False  # restore probe

print("== bash ==")
out = run_bash("echo hello-$((1+1))")
check("bash basic", "hello-2" in out, out)

out = run_bash("exit 3")
check("bash exit code note", "exit code 3" in out, out)

out = run_bash("definitely-not-a-command-xyz")
check("bash 127 hint", "command not found" in out, out)

out = run_bash("grep zzz /etc/hosts")
check("bash benign grep exit", "No matches found (not an error)" in out, out)

out = run_bash("printf '\\033[31mred\\033[0m plain'")
check("bash strips ANSI", "red plain" in out and "\x1b" not in out, repr(out[:60]))

out = run_bash("yes 0123456789 | head -c 200000")
check("bash truncates output", "output truncated" in out and len(out) < 60_000, f"len={len(out)}")
remove_full_output_log(out)

print("== bash: bounded memory on huge output ==")
start = time.time()
out = run_bash("head -c 5000000 /dev/zero | tr '\\0' 'z'; echo; echo FINAL-LINE-SENTINEL", timeout=60)
elapsed = time.time() - start
check("bash 5MB output bounded", len(out) < 60_000, f"len={len(out)}")
check("bash 5MB truncation notice", "output truncated" in out and "full output:" in out, out[-300:])
check("bash 5MB tail preserved", "FINAL-LINE-SENTINEL" in out, out[-300:])
check("bash 5MB reasonable time", elapsed < 20, f"{elapsed:.1f}s")
remove_full_output_log(out)

marker = f"aios-orphan-{os.getpid()}"
start = time.time()
out = run_bash(
    f"python3 -c 'import time; time.sleep(300)' & echo started-{marker}; wait",
    timeout=2,
)
elapsed = time.time() - start
check("bash timeout returns promptly", elapsed < 10, f"{elapsed:.1f}s")
check("bash timeout notice", "timed out after 2s" in out, out)
check("bash timeout partial output kept", f"started-{marker}" in out, out)
time.sleep(0.3)
survivors = subprocess.run(
    ["pgrep", "-f", "time.sleep(300)"], capture_output=True, text=True
).stdout.strip()
check("bash timeout killed children", survivors == "", f"survivors: {survivors}")

print("== processes ==")
info = processes.process_spawn(cwd=str(TMP))
check("process spawn", "process_id" in info, info)
pid = info["process_id"]

processes.process_send(pid, command="echo marker-$((40+2)) && sleep 0.1")
polled = processes.process_poll(pid, wait=10)
check("process poll wait completes", polled["command"]["status"] == "completed", polled["command"])
check("process exit code", polled["command"]["exit_code"] == 0, polled["command"])
check("process output present", "marker-42" in polled["output"], repr(polled["output"][-200:]))
check("process markers scrubbed", "__AIOS_CMD" not in polled["output"] and "__aios_exit_code" not in polled["output"], repr(polled["output"]))

processes.process_send(pid, command="exit 7")
time.sleep(0.5)
polled = processes.process_poll(pid, wait=5)
check("process nonzero exit captured", polled["command"]["exit_code"] == 7, polled["command"])

out = processes.process_kill(pid, signal="SIGFOO")
check("process signal allowlist", "unsupported signal" in out.get("error", ""), out)

spawned = [pid]
cap_error = None
for _ in range(10):
    result = processes.process_spawn(cwd=str(TMP))
    if "error" in result:
        cap_error = result["error"]
        break
    spawned.append(result["process_id"])
check("process session cap", cap_error is not None and "too many process sessions" in cap_error, cap_error)

for sid in spawned:
    processes.process_kill(sid, signal="SIGKILL")

print("== shutdown cleanup ==")
info = processes.process_spawn(cwd=str(TMP))
check("cleanup spawn", "process_id" in info, info)
shell_pid = info["pid"]

processes.close_all_processes()
check("close_all clears sessions", processes.process_list() == [], processes.process_list())
time.sleep(0.3)
try:
    os.kill(shell_pid, 0)
    shell_dead = False
except ProcessLookupError:
    shell_dead = True
check("close_all killed shell", shell_dead, f"pid {shell_pid} alive")

from aios_core.initialize import shutdown_runtime  # noqa: E402

info = processes.process_spawn(cwd=str(TMP))
shutdown_runtime(stop_crons=False)
check("shutdown_runtime closes sessions", processes.process_list() == [], processes.process_list())

print()
print(f"{PASS} passed, {len(FAIL)} failed")
if FAIL:
    print("failures:", *FAIL, sep="\n  - ")
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if FAIL else 0)
