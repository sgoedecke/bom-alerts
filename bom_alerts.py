#!/usr/bin/env python3
"""Track Australian Bureau of Meteorology warnings as they start and end.

Fetches every product XML from the BOM anonymous FTP directory, keeps the ones
that are warnings (product-type W), and compares them against the stored set
of active warnings in data/active.json. Each warning that starts or ends is
recorded as an event in log/YYYY/MM.jsonl. With --commit, every event is
committed to git separately.

Stdlib only.
"""

import argparse
import datetime as dt
import ftplib
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET

FTP_HOST = "ftp.bom.gov.au"
FTP_DIR = "/anon/gen/fwo"
PRODUCT_RE = re.compile(r"^ID[A-Z][0-9]+\.xml$")
ENDED_PHASES = {"CAN", "FIN", "EXP"}
STATES = {
    "N": "NSW",
    "V": "VIC",
    "Q": "QLD",
    "S": "SA",
    "W": "WA",
    "T": "TAS",
    "D": "NT",
    "Y": "AUS",
    "Z": "AUS",
}

# Sanity limits so a broken listing or a flaky run never looks like
# "every warning ended".
MIN_LISTED_PRODUCTS = 100
MAX_FAILED_FRACTION = 0.1

ROOT = os.path.dirname(os.path.abspath(__file__))
ACTIVE_PATH = os.path.join("data", "active.json")
LOG_DIR = "log"


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# Fetching
# --------------------------------------------------------------------------


def connect():
    ftp = ftplib.FTP(FTP_HOST, timeout=60)
    ftp.login()
    ftp.cwd(FTP_DIR)
    return ftp


def retry(fn, attempts, what):
    delay = 2
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except (ftplib.all_errors) as e:
            if attempt == attempts:
                raise
            log(f"{what} failed ({e!r}), retry {attempt}/{attempts - 1} in {delay}s")
            time.sleep(delay)
            delay *= 2


def list_products():
    def attempt():
        ftp = connect()
        try:
            return ftp.nlst()
        finally:
            try:
                ftp.quit()
            except ftplib.all_errors:
                pass

    names = retry(attempt, 5, "FTP listing")
    return sorted(n for n in names if PRODUCT_RE.match(n))


def fetch_all(names, workers=8):
    """Fetch files in parallel. Returns ({name: bytes}, [failed names])."""
    work = queue.Queue()
    for n in names:
        work.put(n)
    results = {}
    failed = []
    lock = threading.Lock()

    def worker():
        ftp = None
        while True:
            try:
                name = work.get_nowait()
            except queue.Empty:
                break
            data = None
            for attempt in range(3):
                try:
                    if ftp is None:
                        ftp = connect()
                    buf = bytearray()
                    ftp.retrbinary("RETR " + name, buf.extend)
                    data = bytes(buf)
                    break
                except ftplib.all_errors as e:
                    log(f"fetch {name} failed ({e!r}), attempt {attempt + 1}/3")
                    try:
                        if ftp is not None:
                            ftp.close()
                    except ftplib.all_errors:
                        pass
                    ftp = None
                    time.sleep(2 * (attempt + 1))
            with lock:
                if data is None:
                    failed.append(name)
                else:
                    results[name] = data
        if ftp is not None:
            try:
                ftp.quit()
            except ftplib.all_errors:
                pass

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, sorted(failed)


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _text(el):
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


def _unique(items):
    seen = []
    for i in items:
        if i and i not in seen:
            seen.append(i)
    return seen


def state_for(product_id):
    letter = product_id[2:3]
    return STATES.get(letter, letter)


def parse_product(data):
    """Parse a product XML. Returns a dict, or None if it isn't a warning."""
    root = ET.fromstring(data)
    amoc = root.find("amoc")
    if amoc is None or amoc.findtext("product-type", "").strip() != "W":
        return None
    pid = amoc.findtext("identifier", "").strip()
    warning = root.find("warning")
    title = ""
    headline = ""
    hazards = []
    areas = []
    if warning is not None:
        for t in warning.iter("text"):
            kind = t.get("type")
            if kind == "warning_title" and not title:
                title = _text(t)
            elif kind in ("headline", "warning_headline") and not headline:
                headline = _text(t)
            elif kind == "warning_phenomena":
                hazards.append(_text(t))
        for a in warning.iter("area"):
            if a.get("type") == "region" or a.get("phase") == "CAN":
                continue
            areas.append(a.get("description", "").strip())
        if not areas:
            areas = [a.get("description", "").strip() for a in warning.iter("area")]
    return {
        "id": pid,
        "state": state_for(pid),
        "title": title or pid,
        "headline": headline,
        "service": amoc.findtext("service", "").strip(),
        "sub_service": amoc.findtext("sub-service", "").strip(),
        "phase": amoc.findtext("phase", "").strip(),
        "issued": amoc.findtext("issue-time-utc", "").strip(),
        "hazards": _unique(h for h in hazards if h != "Cancellation"),
        "areas": _unique(areas),
    }


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


def diff(active, listed_ids, current, unknown_ids=()):
    """Work out lifecycle events.

    active:      {id: record} previously stored active warnings
    listed_ids:  set of product IDs present in the FTP listing
    current:     {id: parsed warning} for warnings fetched this run
    unknown_ids: IDs that were listed but could not be fetched/parsed; their
                 previous state is kept as-is.

    Returns a list of (kind, record, reason) tuples, ends before starts.
    """
    unknown_ids = set(unknown_ids)
    ends = []
    starts = []
    for pid in sorted(active):
        if pid in unknown_ids:
            continue
        if pid not in listed_ids:
            ends.append(("end", active[pid], "removed"))
        elif pid in current and current[pid]["phase"] in ENDED_PHASES:
            ends.append(("end", active[pid], "cancelled"))
        elif pid not in current:
            # Listed and fetched, but no longer a warning product.
            ends.append(("end", active[pid], "removed"))
    for pid in sorted(current):
        w = current[pid]
        if pid not in active and w["phase"] not in ENDED_PHASES:
            starts.append(("start", w, None))
    return ends + starts


def commit_message(kind, record):
    verb = "Start" if kind == "start" else "End"
    return f"{verb}: {record['title']} ({record['state']}) [{record['id']}]"


def event_entry(kind, record, reason, now, current):
    entry = {
        "event": kind,
        "detected": now,
        "id": record["id"],
        "state": record["state"],
        "title": record["title"],
        "service": record["service"],
        "sub_service": record["sub_service"],
    }
    if kind == "start":
        entry.update(
            issued=record["issued"],
            phase=record["phase"],
            headline=record["headline"],
            hazards=record["hazards"],
            areas=record["areas"],
        )
    else:
        entry["reason"] = reason
        entry["started"] = record.get("started")
        latest = current.get(record["id"])
        entry["issued"] = latest["issued"] if latest else None
    return entry


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def load_active(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save_active(path, active):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(active, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def append_log(log_dir, entry, when):
    path = os.path.join(log_dir, f"{when:%Y}", f"{when:%m}.jsonl")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return path


def apply_event(active, kind, record, now):
    if kind == "start":
        stored = {k: v for k, v in record.items() if k != "phase"}
        stored["started"] = now
        active[record["id"]] = stored
    else:
        active.pop(record["id"], None)


def git(*args):
    subprocess.run(["git", *args], check=True)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def run(commit=False, workers=8):
    now_dt = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        names = list_products()
    except ftplib.all_errors as e:
        log(f"FTP listing failed, making no changes: {e!r}")
        return 2
    if len(names) < MIN_LISTED_PRODUCTS:
        log(f"Only {len(names)} products listed, which looks wrong; making no changes")
        return 2
    log(f"Listed {len(names)} products")

    files, failed = fetch_all(names, workers=workers)
    current = {}
    for name, data in files.items():
        try:
            w = parse_product(data)
        except ET.ParseError as e:
            log(f"Could not parse {name}: {e}")
            failed.append(name)
            continue
        if w is not None:
            current[w["id"]] = w
    if len(failed) > MAX_FAILED_FRACTION * len(names):
        log(f"{len(failed)} of {len(names)} products failed; making no changes")
        return 2
    log(f"Found {len(current)} warning products ({len(failed)} failed)")

    listed_ids = {n[:-4] for n in names}
    unknown_ids = {n[:-4] for n in failed}
    active = load_active(ACTIVE_PATH)
    events = diff(active, listed_ids, current, unknown_ids)
    if not events:
        log("No warnings started or ended")
        return 0

    for kind, record, reason in events:
        apply_event(active, kind, record, now)
        save_active(ACTIVE_PATH, active)
        log_path = append_log(LOG_DIR, event_entry(kind, record, reason, now, current), now_dt)
        msg = commit_message(kind, record)
        print(msg)
        if commit:
            git("add", ACTIVE_PATH, log_path)
            git("commit", "--quiet", "-m", msg)
    return 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--commit", action="store_true", help="git commit each event separately")
    p.add_argument("--workers", type=int, default=8, help="parallel FTP connections")
    args = p.parse_args()
    os.chdir(ROOT)
    sys.exit(run(commit=args.commit, workers=args.workers))


if __name__ == "__main__":
    main()
