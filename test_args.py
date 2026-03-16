
import asyncio
import os

async def test():
    user_text = "-v"
    cmd = ["gemini", "--output-format", "text", "--approval-mode", "yolo", f"--prompt={user_text}"]
    process = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    print("STDOUT:", stdout.decode())
    print("STDERR:", stderr.decode())
    print("RETCODE:", process.returncode)

asyncio.run(test())
