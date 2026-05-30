#!/usr/bin/env python3
import subprocess
import time
import sys

password_file = f"{sys.argv[1]}/.ssh/.tunnelpass"
with open(password_file) as f:
    password = f.read().strip()

proc = subprocess.Popen(
    ["sshpass", "-p", password,
     "ssh", "-N", "-o", "StrictHostKeyChecking=no",
     "admin@170.170.11.26",
     "-L", "18765:127.0.0.1:8765"],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
)
time.sleep(3)
if proc.poll() is not None:
    print("SSH进程已退出，隧道可能未建立")
else:
    print(f"隧道进程运行中 PID={proc.pid}")
    print("可用 ctrl-c 停止，或运行: kill", proc.pid)
    proc.wait()
