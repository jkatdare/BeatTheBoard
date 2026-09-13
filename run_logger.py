"""
Supervisor for njt_logger.py -- for multi-day unattended runs.

The logger itself already survives API errors, but not process death: a crash,
a network drop that outlives its backoff, or the machine sleeping will end it
silently. Last run stopped after 8 hours and nobody noticed for two days.

This restarts it forever and keeps a heartbeat you can check at a glance.

    python run_logger.py

Also worth doing once, so Windows does not sleep the machine out from under it:
    powercfg /change standby-timeout-ac 0
    powercfg /change hibernate-timeout-ac 0
(Undo later with a nonzero number of minutes, e.g. 30.)
"""

import subprocess
import sys
import time
from datetime import datetime

RESTART_DELAY = 15          # seconds between restarts
HEARTBEAT = "logger_heartbeat.txt"


def stamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def note(msg):
    line = "[" + stamp() + "] " + msg
    print(line, flush=True)
    try:
        with open(HEARTBEAT, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main():
    note("supervisor started; Ctrl-C to stop everything")
    restarts = 0
    while True:
        started = time.time()
        try:
            proc = subprocess.run([sys.executable, "njt_logger.py"])
            code = proc.returncode
        except KeyboardInterrupt:
            note("interrupted by user; exiting")
            return
        except Exception as e:
            note("failed to launch logger: " + type(e).__name__ + ": " + str(e))
            code = -1

        ran = time.time() - started
        restarts += 1
        note("logger exited (code %s) after %.1f min -- restart #%d in %ds"
             % (code, ran / 60.0, restarts, RESTART_DELAY))

        # A logger that dies instantly and repeatedly is misconfigured, not
        # unlucky. Back off so the log does not fill with the same error.
        if ran < 30:
            note("exited almost immediately -- check .env and credentials")
            time.sleep(60)
        else:
            time.sleep(RESTART_DELAY)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        note("supervisor stopped")
