"""
启动 EDR GUI
通过 HisecEndpointAgent.exe cmd ui 命令直接启动 GUI 窗口
"""
import subprocess

edr_path = r"C:\Program Files\HiSec-Endpoint\core\safra\HisecEndpointAgent.exe"

result = subprocess.run(
    [edr_path, "cmd", "ui"],
    capture_output=True,
    text=True
)
print("stdout:", result.stdout)
print("stderr:", result.stderr)
print("returncode:", result.returncode)
