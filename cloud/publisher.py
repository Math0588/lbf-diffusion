"""Diffuseur cloud — publie la réserve sur Instagram, même PC éteint.

Tourne dans GitHub Actions (cron horaire), bibliothèque standard Python uniquement.
API : Instagram API with Instagram Login (graph.instagram.com), permissions instagram_business_basic +
instagram_business_content_publish. La vidéo doit être accessible publiquement : elle est servie par
GitHub Pages depuis ce dépôt (PAGES_BASE_URL).

Garanties :
- LIVE_PUBLISH != "true"  → simulation explicite, jamais de statut « published ».
- Au plus 1 publication par jour (Europe/Paris), pas de rattrapage après panne.
- Identifiant du conteneur persisté AVANT publication ; un timeout donne « publication_uncertain »
  puis une réconciliation (lecture des derniers médias) au lieu d'un nouvel essai aveugle.
- Le jeton n'est jamais écrit dans les journaux.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
RESERVE = ROOT / "reserve"
STATE = ROOT / "state"
TZ = ZoneInfo("Europe/Paris")
GRAPH = "https://graph.instagram.com"
VER = os.environ.get("IG_GRAPH_VERSION", "v24.0")


def env(k, d=""):
    return os.environ.get(k, d).strip()


def now():
    return dt.datetime.now(TZ)


def load(name, default):
    p = STATE / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def save(name, data):
    STATE.mkdir(exist_ok=True)
    (STATE / name).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def log(msg):
    tok = env("IG_ACCESS_TOKEN")
    if tok:
        msg = msg.replace(tok, "***")
    print(f"[{now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status, self.body = status, body


def call(method, path, params=None, timeout=60):
    params = dict(params or {})
    params["access_token"] = env("IG_ACCESS_TOKEN")
    url = path if path.startswith("http") else f"{GRAPH}/{VER}/{path}"
    data = None
    if method == "GET":
        url += "?" + urllib.parse.urlencode(params)
    else:
        data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"))


def notify(kind, text):
    """Exception concrète → fichier d'alertes (visible dans le dépôt et le tableau de bord). Pas de demande
    d'approbation pour les opérations courantes."""
    alerts = load("alerts.json", [])
    alerts.append({"at": now().isoformat(), "kind": kind, "text": text})
    save("alerts.json", alerts[-200:])
    log(f"ALERTE {kind}: {text}")


# ----------------------------------------------------------------------------- jeton

def maybe_refresh_token():
    """Jeton long Instagram : 60 jours, rafraîchissable s'il a ≥ 24 h et n'a pas expiré.
    Le nouveau jeton est écrit dans le secret GitHub via `gh secret set` (PAT dédié, droit Secrets: write)."""
    meta = load("token.json", {})
    last = meta.get("refreshed_at")
    if last and (now() - dt.datetime.fromisoformat(last)).days < 7:
        return
    if env("LIVE_PUBLISH") != "true":
        return
    try:
        r = call("GET", f"{GRAPH}/refresh_access_token", {"grant_type": "ig_refresh_token"})
    except ApiError as e:
        notify("token", f"Rafraîchissement du jeton impossible ({e.status}). Reconnexion du propriétaire "
                        f"probablement nécessaire.")
        return
    new = r.get("access_token")
    if new and env("GH_SECRETS_TOKEN"):
        import subprocess
        p = subprocess.run(["gh", "secret", "set", "IG_ACCESS_TOKEN", "--body", new],
                           env={**os.environ, "GH_TOKEN": env("GH_SECRETS_TOKEN")}, capture_output=True, text=True)
        if p.returncode != 0:
            notify("token", "Nouveau jeton obtenu mais non enregistré dans les secrets GitHub.")
            return
    elif new:
        notify("token", "Jeton rafraîchi côté Meta mais GH_SECRETS_TOKEN absent : l'ancien reste valable "
                        "jusqu'à son expiration, ajouter GH_SECRETS_TOKEN.")
    meta["refreshed_at"] = now().isoformat()
    meta["expires_in_s"] = r.get("expires_in")
    save("token.json", meta)


# ----------------------------------------------------------------------------- publication

def next_episode(queue, pubs):
    done = {p["episode_id"] for p in pubs if p["status"] in ("published", "publication_uncertain", "failed")}
    fresh = []
    for item in queue:
        exp = item.get("expires")
        if exp and dt.datetime.fromisoformat(exp).replace(tzinfo=TZ) < now():
            continue  # actualité périmée : jamais publiée en retard
        fresh.append(item)
    queue = sorted(fresh, key=lambda i: 0 if i.get("urgent") else 1)
    for item in queue:
        if item["episode_id"] not in done and (RESERVE / item["episode_id"] / "video.mp4").exists():
            return item
    return None


def reconcile(pub, user_id):
    """Après un timeout : chercher le média dans les derniers posts avant tout nouvel essai."""
    marker = pub["caption_head"]
    r = call("GET", f"{user_id}/media", {"fields": "id,caption,permalink,timestamp", "limit": 10})
    for m in r.get("data", []):
        if (m.get("caption") or "").startswith(marker):
            pub.update(status="published", media_id=m["id"], permalink=m.get("permalink"),
                       published_at=m.get("timestamp"), reconciled=True)
            return True
    return False


def publish_one(item, pubs):
    user_id = env("IG_USER_ID")
    ep_id = item["episode_id"]
    folder = RESERVE / ep_id
    caption = (folder / "caption.txt").read_text(encoding="utf-8")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    base = env("PAGES_BASE_URL").rstrip("/")
    video_url = f"{base}/reserve/{ep_id}/video.mp4"
    cover_url = f"{base}/reserve/{ep_id}/cover.jpg"
    pub = {"episode_id": ep_id, "status": "publishing", "attempt_at": now().isoformat(),
           "video_sha256": manifest.get("video_sha256"), "caption_head": caption[:60]}

    if env("LIVE_PUBLISH") != "true":
        pub.update(status="simulated", note="LIVE_PUBLISH=false : aucune requête envoyée à Instagram",
                   would_post={"video_url": video_url, "cover_url": cover_url, "caption_chars": len(caption)})
        pubs.append(pub)
        log(f"SIMULATION {ep_id} → {video_url}")
        return pub

    lim = call("GET", f"{user_id}/content_publishing_limit", {"fields": "quota_usage,config"})
    log(f"Quota : {lim}")
    pubs.append(pub)
    save("publications.json", pubs)
    c = call("POST", f"{user_id}/media", {"media_type": "REELS", "video_url": video_url, "caption": caption,
                                           "cover_url": cover_url, "share_to_feed": "true"})
    pub["container_id"] = c["id"]
    save("publications.json", pubs)
    deadline = time.time() + 15 * 60
    status = ""
    while time.time() < deadline:
        s = call("GET", c["id"], {"fields": "status_code,status"})
        status = s.get("status_code")
        if status == "FINISHED":
            break
        if status in ("ERROR", "EXPIRED"):
            pub.update(status="failed", error=s.get("status"))
            notify("publication", f"{ep_id} : conteneur {status} ({s.get('status')})")
            return pub
        time.sleep(20)
    if status != "FINISHED":
        pub.update(status="failed", error="traitement Meta trop long (15 min)")
        notify("publication", f"{ep_id} : traitement non terminé, réessai au prochain créneau")
        pub["retry"] = True
        return pub
    try:
        r = call("POST", f"{user_id}/media_publish", {"creation_id": c["id"]}, timeout=90)
        pub.update(status="published", media_id=r["id"], published_at=now().isoformat())
        try:
            m = call("GET", r["id"], {"fields": "permalink"})
            pub["permalink"] = m.get("permalink")
        except ApiError:
            pass
    except (urllib.error.URLError, TimeoutError, ApiError) as e:
        pub.update(status="publication_uncertain", error=str(e)[:300])
        save("publications.json", pubs)
        time.sleep(30)
        if not reconcile(pub, user_id):
            notify("publication", f"{ep_id} : publication incertaine, aucune republication automatique.")
    return pub


def publish_youtube(item):
    """Module optionnel YouTube Shorts : jamais bloquant pour Instagram, jamais deux fois le même épisode."""
    sys.path.insert(0, str(Path(__file__).parent))
    import youtube
    if not youtube.enabled():
        return
    yts = load("youtube.json", [])
    if any(y["episode_id"] == item["episode_id"] and y.get("video_id") for y in yts):
        return
    folder = RESERVE / item["episode_id"]
    caption = (folder / "caption.txt").read_text(encoding="utf-8")
    title = caption.splitlines()[0][:90]
    try:
        r = youtube.upload(folder / "video.mp4", title, caption, ["satire", "humour", "politique", "danscetteville"])
        yts.append({"episode_id": item["episode_id"], "at": now().isoformat(), **r})
        log(f"YouTube : {r.get('url')} ({r.get('privacy')})")
    except Exception as e:
        yts.append({"episode_id": item["episode_id"], "at": now().isoformat(), "error": str(e)[:300]})
        notify("youtube", f"{item['episode_id']} : envoi YouTube échoué ({str(e)[:120]})")
    save("youtube.json", yts)


def collect_metrics(pubs):
    """Mesures à 24 h, 72 h, 7 j — uniquement ce que l'API expose ; une donnée absente reste absente."""
    if env("LIVE_PUBLISH") != "true":
        return
    snaps = load("metrics.json", [])
    have = {(s["media_id"], s["window"]) for s in snaps}
    metrics = "reach,views,likes,comments,shares,saved,total_interactions,ig_reels_avg_watch_time"
    for p in pubs:
        if p.get("status") != "published" or not p.get("published_at"):
            continue
        age_h = (now() - dt.datetime.fromisoformat(p["published_at"].replace("Z", "+00:00"))).total_seconds() / 3600
        for win, h in (("24h", 24), ("72h", 72), ("7j", 168)):
            if age_h >= h and (p["media_id"], win) not in have:
                try:
                    r = call("GET", f"{p['media_id']}/insights", {"metric": metrics})
                    vals = {d["name"]: (d.get("values") or [{}])[0].get("value") for d in r.get("data", [])}
                    snaps.append({"media_id": p["media_id"], "episode_id": p["episode_id"], "window": win,
                                  "at": now().isoformat(), "values": vals})
                except ApiError as e:
                    snaps.append({"media_id": p["media_id"], "episode_id": p["episode_id"], "window": win,
                                  "at": now().isoformat(), "error": e.body[:200]})
    save("metrics.json", snaps)


def cleanup(pubs):
    """Supprime de la réserve les vidéos publiées depuis > 3 jours (dépôt léger, Pages < 1 Go)."""
    import shutil
    for p in pubs:
        if p.get("status") == "published" and p.get("published_at"):
            age = now() - dt.datetime.fromisoformat(p["published_at"].replace("Z", "+00:00"))
            d = RESERVE / p["episode_id"]
            if age.days >= 3 and d.exists():
                shutil.rmtree(d)
                log(f"Réserve nettoyée : {p['episode_id']}")


def main():
    control = load("control.json", {"paused": False, "slot": "18:30"})
    pubs = load("publications.json", [])
    queue = json.loads((RESERVE / "queue.json").read_text(encoding="utf-8")) if (RESERVE / "queue.json").exists() else []
    missing = [k for k in ("IG_ACCESS_TOKEN", "IG_USER_ID", "PAGES_BASE_URL") if not env(k)]
    if env("LIVE_PUBLISH") == "true" and missing:
        notify("configuration", f"Configuration requise : secrets manquants {missing}")
        return 0
    if control.get("paused"):
        log("Pause générale active : aucune requête, aucune publication.")
        return 0
    maybe_refresh_token()
    collect_metrics(pubs)
    cleanup(pubs)
    today = now().date().isoformat()
    slots = control.get("slots") or [x.strip() for x in env("PUBLISH_SLOTS", "12:30,19:00").split(",") if x.strip()]
    done_today = [p for p in pubs if p.get("attempt_at", "")[:10] == today and
                  p["status"] in ("published", "publishing", "simulated", "publication_uncertain")]
    passed = [s for s in slots if (now().hour, now().minute) >= tuple(map(int, s.split(":")))]
    if "--force" not in sys.argv:
        if len(done_today) >= len(passed):
            log(f"Rien à publier maintenant (créneaux {', '.join(slots)} Europe/Paris ; {len(done_today)} fait(s)).")
            return 0
        if done_today:
            last = max(dt.datetime.fromisoformat(p["attempt_at"]) for p in done_today)
            if (now() - last).total_seconds() < 3 * 3600:
                log("Moins de 3 h depuis la dernière publication : pas de rafale après une panne.")
                return 0
    item = next_episode(queue, pubs)
    if not item:
        notify("reserve", "Réserve vide : aucune vidéo prête. Allumer le PC pour produire.")
        save("publications.json", pubs)
        return 0
    remaining = len([q for q in queue if (RESERVE / q["episode_id"] / "video.mp4").exists()]) - 1
    if remaining < 3:
        notify("reserve", f"Réserve basse : {remaining} vidéo(s) après celle-ci.")
    pub = publish_one(item, pubs)
    save("publications.json", pubs)
    publish_youtube(item)
    log(f"Résultat {pub['episode_id']} : {pub['status']} {pub.get('permalink', '')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
