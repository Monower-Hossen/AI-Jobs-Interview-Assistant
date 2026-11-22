import subprocess
import sys

result = subprocess.run(
    [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short"],
    cwd="C:\\backup\\AI-Interview-Assistant",
    capture_output=True,
    text=True,
    check=False,
)

output = result.stdout + result.stderr

import re

passed = re.findall(r'(\d+?) passed', output)
failed = re.findall(r'(\d+?) failed', output)
error = re.findall(r'(\d+?) error', output)
skipped = re.findall(r'(\d+?) skipped', output)

# Count test lines
test_lines = re.findall(r'TESTS.*?(\d+?) ', output, re.IGNORECASE)

print("=== Test Collection and Results ===")
print("Collected: 135 tests (from --collect-only)")
print()

# Parse the output for pass/fail counts
lines = output.split('\n')
for line in lines:
    if 'passed' in line and 'failed' not in line:
        print(f"Pass line: {line.strip()}")
    if 'failed' in line:
        print(f"Fail line: {line.strip()}")
    if 'error' in line and 'passed' not in line:
        print(f"Error line: {line.strip()}")
    if 'skipped' in line:
        print(f"Skip line: {line.strip()}")

print()
print(f"Return code: {result.returncode}")