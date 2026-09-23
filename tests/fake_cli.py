"""Stand-in for an allowed command-line program (like lark-cli): prints what it got as JSON.
`--exit N` exits with N, `--sleep S` waits S seconds first."""
import json
import os
import sys
import time

args = sys.argv[1:]
if "--sleep" in args:
    time.sleep(float(args[args.index("--sleep") + 1]))
stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
sys.stdout.write(json.dumps({"args": args, "cwd": os.getcwd(), "stdin": stdin}, ensure_ascii=False) + "\n")
if "--exit" in args:
    sys.stderr.write("failed on purpose\n")
    sys.exit(int(args[args.index("--exit") + 1]))
