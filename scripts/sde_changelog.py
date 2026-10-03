#!/usr/bin/env python3
"""Descarga todos los changelogs del SDE de EVE y los une con schema-changelog.yaml.

Uso:   python sde_changelog.py [directorio_salida]
Requiere: Python 3.8+, pyyaml (pip install pyyaml)

Regla de union (verificada con los datos reales):
  una entrada del YAML con afterBuildNumber N corresponde al PRIMER build posterior a N.
  Si N es anterior a la cadena descargada (p.ej. 2960198), se asigna al primer build de la
  cadena y se marca como pre_chain (puede abarcar mas de un build).

Salida (por defecto ./sde_changelog_out):
  changes/<build>.jsonl     archivos originales
  builds.csv                cadena de builds
  records.csv               una fila por (build, key, op, id)
  keys_summary.csv          claves distintas y cuantos builds las usan
  joined.csv                cada entrada del YAML unida con el JSONL (status OK/MISMATCH/NO_TARGET)
  orphans.csv               cambios de estructura del JSONL que NO explica ninguna entrada del YAML
  sde_changelog.sqlite      las mismas tablas en SQLite
"""
import csv, json, os, sqlite3, sys, time, urllib.request, urllib.error
from collections import Counter, defaultdict

BASE = os.environ.get("SDE_BASE", "https://developers.eveonline.com/static-data/tranquility")
OUT = sys.argv[1] if len(sys.argv) > 1 else "sde_changelog_out"
os.makedirs(os.path.join(OUT, "changes"), exist_ok=True)
STRUCT_OPS = {"schemaChanged", "fileAdded", "fileRemoved", "fileRenamed"}


def get(url, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": os.environ.get("SDE_USER_AGENT", "sde-deltas")})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def jsonl(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


def write_csv(name, header, data):
    with open(os.path.join(OUT, name), "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(data)


# 1. Cadena de builds hacia atras desde latest.jsonl
latest = jsonl(get(f"{BASE}/latest.jsonl"))
build = next(r for r in latest if r.get("_key") == "sde")["buildNumber"]
chain = []  # (build, last_build, date, records)
while build:
    path = os.path.join(OUT, "changes", f"{build}.jsonl")
    if os.path.exists(path):
        text = open(path, encoding="utf-8").read()
    else:
        text = get(f"{BASE}/changes/{build}.jsonl")
        if text is None:
            print(f"fin de la cadena: no existe changes/{build}.jsonl")
            break
        open(path, "w", encoding="utf-8").write(text)
    recs = jsonl(text)
    meta = next((r for r in recs if r.get("_key") == "_meta"), {})
    chain.append((build, meta.get("lastBuildNumber"), meta.get("releaseDate"), recs))
    print(build, "<-", meta.get("lastBuildNumber"), len(recs), "registros")
    build = meta.get("lastBuildNumber")
chain.reverse()
builds = [c[0] for c in chain]
dates = {c[0]: c[2] for c in chain}
order = {b: i for i, b in enumerate(builds)}

# 2. Aplanar
rows, keycount = [], Counter()
ops_by = defaultdict(set)  # (build, key) -> {ops}
for b, last, date, recs in chain:
    for r in recs:
        k = r.get("_key")
        if k == "_meta":
            continue
        for op, val in r.items():
            if op == "_key":
                continue
            keycount[(k, op)] += 1
            ops_by[(b, k)].add(op)
            vals = val if isinstance(val, list) else [val]
            for i in vals:
                rows.append((b, date, k, op, i if isinstance(i, (int, str)) else json.dumps(i)))

write_csv("builds.csv", ["build", "last_build", "release_date"], [(b, l, d) for b, l, d, _ in chain])
write_csv("records.csv", ["build", "release_date", "key", "op", "id"], rows)
write_csv("keys_summary.csv", ["key", "op", "builds"], sorted((k, o, n) for (k, o), n in keycount.items()))


# 3. Unir YAML con JSONL
def expected_ops(kind, detail):
    d = detail.lower()
    if kind == "fields":
        return {"schemaChanged"}
    if d.startswith('"added') or d.startswith("added"):
        return {"fileAdded"}
    if "renamed" in d:
        return {"fileRenamed"}
    if "removed" in d or "merged into" in d:
        return {"fileRemoved"}
    if "reworked" in d:
        return {"schemaChanged", "fileAdded"}
    return STRUCT_OPS


joined, explained = [], set()  # explained: (build, key, op)
try:
    import yaml
    doc = yaml.safe_load(get(f"{BASE}/schema-changelog.yaml"))
    for e in doc["schemaChangelog"]:
        after, ydate = e["afterBuildNumber"], str(e.get("date"))
        for kind in ("files", "fields"):
            sec = e.get(kind) or {}
            ch = sec.get("changes", sec) if isinstance(sec, dict) else {}
            for k, v in (ch or {}).items():
                detail = json.dumps(v, ensure_ascii=False)
                exp = expected_ops(kind, detail)
                later = [b for b in builds if b > after]
                target = later[0] if later else None
                pre = after not in order
                if target is None:
                    status, here = "NO_TARGET", ""
                else:
                    here = ops_by.get((target, k), set())
                    hit = here & exp
                    if hit:
                        status = "OK_pre_chain" if pre else "OK"
                        for op in hit:
                            explained.add((target, k, op))
                    else:
                        status = "MISMATCH"
                other = sorted(b for b in builds if ops_by.get((b, k), set()) & exp)
                joined.append((after, target, dates.get(target), ydate, kind, k, detail,
                               ";".join(sorted(exp)), status, ";".join(sorted(here)) if target else "",
                               ";".join(map(str, other)) if status == "MISMATCH" else ""))
except ImportError:
    print("pyyaml no instalado: se omite la union con el YAML (pip install pyyaml)")

J_HDR = ["yaml_afterBuild", "jsonl_target_build", "target_date", "yaml_date", "kind", "key", "yaml_detail",
         "expected_ops", "status", "ops_in_target_build", "other_builds_with_expected_op"]
write_csv("joined.csv", J_HDR, joined)

orphans = sorted((b, k, op) for (b, k), ops in ops_by.items() for op in ops
                 if op in STRUCT_OPS and (b, k, op) not in explained)
write_csv("orphans.csv", ["build", "key", "op"], orphans)

# 4. SQLite
db = sqlite3.connect(os.path.join(OUT, "sde_changelog.sqlite"))
db.executescript("""
DROP TABLE IF EXISTS builds; DROP TABLE IF EXISTS records;
DROP TABLE IF EXISTS joined; DROP TABLE IF EXISTS orphans;
CREATE TABLE builds(build INTEGER PRIMARY KEY, last_build INTEGER, release_date TEXT);
CREATE TABLE records(build INTEGER, release_date TEXT, key TEXT, op TEXT, id TEXT);
CREATE INDEX ix_records ON records(key, op, build);
CREATE TABLE joined(yaml_after_build INTEGER, target_build INTEGER, target_date TEXT, yaml_date TEXT,
  kind TEXT, key TEXT, yaml_detail TEXT, expected_ops TEXT, status TEXT, ops_in_target TEXT, other_builds TEXT);
CREATE TABLE orphans(build INTEGER, key TEXT, op TEXT);
""")
db.executemany("INSERT INTO builds VALUES (?,?,?)", [(b, l, d) for b, l, d, _ in chain])
db.executemany("INSERT INTO records VALUES (?,?,?,?,?)", [tuple(str(x) if i == 4 else x for i, x in enumerate(r)) for r in rows])
db.executemany("INSERT INTO joined VALUES (?,?,?,?,?,?,?,?,?,?,?)", joined)
db.executemany("INSERT INTO orphans VALUES (?,?,?)", orphans)
db.commit()

print(f"\nbuilds: {len(chain)}  registros: {len(rows)}  claves (key,op): {len(keycount)}")
print("operaciones vistas:", sorted({o for _, o in keycount}))
if joined:
    print("estado de la union YAML->JSONL:", dict(Counter(r[8] for r in joined)))
    print("cambios de estructura del JSONL sin entrada en el YAML:", len(orphans))
print("salida en", os.path.abspath(OUT))
