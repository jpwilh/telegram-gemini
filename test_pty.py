import pty
import os
import subprocess

def read(fd):
    data = os.read(fd, 1024)
    return data

master_fd, slave_fd = pty.openpty()
p = subprocess.Popen(["gemini", "--output-format", "stream-json", "-p", "Hi"], 
                     stdout=slave_fd, stderr=slave_fd, stdin=slave_fd, text=False)
os.close(slave_fd)

output = b""
while p.poll() is None:
    try:
        chunk = os.read(master_fd, 1024)
        if chunk:
            output += chunk
    except OSError:
        break

print(f"RAW OUTPUT: {output}")
