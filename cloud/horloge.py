"""Horloge du diffuseur : déclenche publish.yml à chaque créneau de state/control.json (+ relance 30 min après),
puis se relance elle-même toutes les ~5 h 40. Tourne seulement si le dépôt est public (minutes gratuites) ;
démarre toute seule quand le dépôt passe en public (événement GitHub « public »). Aucun Claude, aucun jeton."""
import datetime as dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Paris")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
LIFETIME_S = float(os.environ.get("HORLOGE_MINUTES", "340")) * 60
DEFAULT_SLOTS = ["08:00", "12:00", "17:00", "20:00", "22:00"]


def log(m):
    print(f"[horloge {dt.datetime.now(TZ):%d/%m %H:%M}] {m}", flush=True)


def gh(*a):
    return subprocess.run(["gh", *a], capture_output=True, text=True)


def slots_today():
    subprocess.run(["git", "pull", "-q", "--ff-only"], capture_output=True)
    try:
        c = json.loads(Path("state/control.json").read_text(encoding="utf-8"))
    except Exception:
        c = {}
    if c.get("paused"):
        return []
    today = dt.datetime.now(TZ).date().isoformat()
    b = next((x for x in c.get("bursts", []) if x.get("date") == today), None)
    return [b["at"]] if b else (c.get("slots") or DEFAULT_SLOTS)


def main():
    t0, fired, slots, slots_at = time.time(), set(), DEFAULT_SLOTS, 0.0
    log("démarrage")
    while time.time() - t0 < LIFETIME_S:
        now = dt.datetime.now(TZ)
        if time.time() - slots_at > 3600:
            slots, slots_at = slots_today(), time.time()
        for s in slots:
            h, m = map(int, s.split(":"))
            base = now.replace(hour=h, minute=m, second=0, microsecond=0)
            for k, delay in enumerate((0, 30)):
                at = base + dt.timedelta(minutes=delay)
                key = (now.date(), s, k)
                if at <= now < at + dt.timedelta(minutes=12) and key not in fired:
                    fired.add(key)
                    r = gh("workflow", "run", "publish.yml", "-R", REPO, "--ref", "main")
                    log(f"créneau {s}{' (relance)' if k else ''} : "
                        + ("publication déclenchée" if r.returncode == 0 else r.stderr.strip()[:200]))
        time.sleep(300)
    r = gh("workflow", "run", "horloge.yml", "-R", REPO, "--ref", "main")
    log("relève lancée" if r.returncode == 0 else f"relève impossible : {r.stderr.strip()[:200]}")


if __name__ == "__main__":
    sys.exit(main())
