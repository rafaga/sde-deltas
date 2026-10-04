#!/usr/bin/env python3
"""Procesa los builds nuevos del SDE de EVE: un delta consecutivo por build en deltas/<build>/.

Uso:
  python scripts/process_latest.py [--deltas-dir deltas] [--work-dir work] [--max-builds N]
                                   [--first-build 3031812] [--build N] [--changelog-only] [--dry-run] [--keep-zips]

Por defecto compara TODAS las tablas de cada par de builds (--verify-all de sde_delta.py): asi se detectan
cambios que el changelog de CCP no lista, en el momento y sin reprocesar builds antiguos. Medido: cuesta ~15 %
mas que comparar solo las tablas del changelog (el tiempo se va en leer los zips). --changelog-only compara solo
las tablas que lista el changelog (mas rapido; el manifest lo registra como "changelog-listed").

Idempotente: consulta latest.jsonl, sigue _meta.lastBuildNumber hacia atras hasta un build ya
procesado (o hasta --first-build) y procesa los pendientes en orden ascendente. Cada build se escribe
primero en deltas/<build>.tmp y se renombra al terminar, asi que nunca queda un directorio a medias.
Si un build falla, se detiene (no se salta: la cadena debe ser consecutiva) y sale con codigo 1.

Por build escribe en deltas/<build>/: delta.jsonl.gz, manifest.json, summary.csv, verification.csv,
schema_delta.csv. Despues regenera deltas/index.json y CHANGELOG.md a partir de los manifest
(salida determinista: sin fechas de ejecucion, para que re-ejecutar no produzca commits).

Variables de entorno: SDE_BASE (servidor), SDE_USER_AGENT (debe incluir un contacto).
"""
import argparse, hashlib, json, os, shutil, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sde_delta as s  # noqa: E402

FORMAT_VERSION = 1
FIRST_BUILD = 3031812   # primer delta publicado (base 3030547); la transicion 2960198 -> 3030547 no se incluye
SOURCE = "https://developers.eveonline.com/static-data/tranquility"
NOTICE = ('© 2014 CCP hf. All rights reserved. "EVE", "EVE Online", "CCP", and all related logos and images '
          "are trademarks or registered trademarks of CCP hf. This data is derived from the EVE Online Static "
          "Data Export and is the property of CCP hf.; see NOTICE. No official affiliation or endorsement by "
          "CCP Games is stated or implied.")


def changelog_meta(build, changes_dir):
    """_meta del changelog de un build ({'lastBuildNumber', 'releaseDate', ...}) o None si no existe."""
    path = os.path.join(changes_dir, f"{build}.jsonl")
    if not os.path.exists(path):
        text = s.fetch(f"{s.BASE}/changes/{build}.jsonl")
        if text is None:
            return None
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    with open(path, encoding="utf-8") as f:
        return next((r for r in s.jsonl(f.read()) if r.get("_key") == "_meta"), None)


def processed_builds(deltas_dir):
    if not os.path.isdir(deltas_dir):
        return set()
    return {int(d) for d in os.listdir(deltas_dir)
            if d.isdigit() and os.path.exists(os.path.join(deltas_dir, d, "manifest.json"))}


def latest_build():
    text = s.fetch(f"{s.BASE}/latest.jsonl")
    if text is None:
        raise s.DeltaError("no se pudo leer latest.jsonl")
    return next(r for r in s.jsonl(text) if r.get("_key") == "sde")["buildNumber"]


def pending_builds(latest, done, first, changes_dir):
    """[(build, build_anterior)] pendientes en orden ascendente, siguiendo la cadena desde latest."""
    out, cur = [], latest
    while cur >= first and cur not in done:
        meta = changelog_meta(cur, changes_dir)
        if meta is None:
            raise s.DeltaError(f"no existe changes/{cur}.jsonl: la cadena de builds se rompe en {cur}")
        out.append((cur, meta["lastBuildNumber"]))
        cur = meta["lastBuildNumber"]
    return out[::-1]


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_manifest(out_dir, build, last, release_date, res, verify_all):
    errors = res["verification"]["errors"]
    status = "ok" if errors == 0 and res["complete"] else "warn"
    tables = {}
    for t in res["tables"]:
        if not (t["added"] or t["removed"] or t["changed"] or t["schema_ops"] or t["flags"]):
            continue
        entry = {"added": t["added"], "removed": t["removed"], "changed": t["changed"]}
        if t["schema_ops"]:
            entry["schemaOps"] = t["schema_ops"]
        if t["flags"]:
            entry["changelogFlags"] = t["flags"]
        if t["renamed_from"]:
            entry["renamedFrom"] = t["renamed_from"]
        tables[t["table"]] = entry
    files = {n: {"bytes": os.path.getsize(os.path.join(out_dir, n)), "sha256": sha256(os.path.join(out_dir, n))}
             for n in sorted(os.listdir(out_dir)) if n != "manifest.json"}
    manifest = {
        "formatVersion": FORMAT_VERSION,
        "build": build,
        "lastBuild": last,
        "releaseDate": release_date,
        "source": SOURCE,
        "notice": NOTICE,
        "verification": {"status": status, "errors": errors, "info": res["verification"]["info"],
                         "changelogCoverage": "complete" if res["complete"] else "incomplete",
                         "mode": "verify-all" if verify_all else "changelog-listed",
                         "errorDetails": res["verification"]["error_details"][:20]},
        "totals": {"tablesCompared": res["tables_compared"], "tablesChanged": len(tables),
                   "added": sum(t["added"] for t in tables.values()),
                   "removed": sum(t["removed"] for t in tables.values()),
                   "changed": sum(t["changed"] for t in tables.values()),
                   "schemaOps": sum(sum(t.get("schemaOps", {}).values()) for t in tables.values()),
                   "deltaLines": res["delta_lines"]},
        "tables": tables,
        "files": files,
    }
    if res.get("counts") is not None:
        manifest["counts"] = res["counts"]   # registros de cada tabla del build, cambie o no
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")
    return manifest


def process_build(build, last, args, release_date):
    final = os.path.join(args.deltas_dir, str(build))
    tmp = final + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    verify_all = not args.changelog_only
    res = s.run(last, build, args.work_dir, changes_dir=os.path.join(args.work_dir, "changes"),
                verify_all=verify_all, gzip_delta=True, zips_dir=os.path.join(args.work_dir, "zips"),
                out_dir=tmp)
    manifest = write_manifest(tmp, build, last, release_date, res, verify_all)
    shutil.rmtree(final, ignore_errors=True)   # reproceso (--build) de un build ya existente
    os.replace(tmp, final)
    return manifest


def load_manifests(deltas_dir):
    out = []
    for b in sorted(processed_builds(deltas_dir)):
        with open(os.path.join(deltas_dir, str(b), "manifest.json"), encoding="utf-8") as f:
            out.append(json.load(f))
    return out


def write_index(deltas_dir, manifests):
    index = {"formatVersion": FORMAT_VERSION, "source": SOURCE,
             "firstBuild": manifests[0]["build"] if manifests else None,
             "latestBuild": manifests[-1]["build"] if manifests else None,
             "builds": [{"build": m["build"], "lastBuild": m["lastBuild"], "releaseDate": m["releaseDate"],
                         "verification": m["verification"]["status"],
                         "deltaBytes": m["files"]["delta.jsonl.gz"]["bytes"]} for m in manifests]}
    with open(os.path.join(deltas_dir, "index.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(index, f, ensure_ascii=False, indent=1)
        f.write("\n")


def write_changelog(path, manifests):
    lines = ["# Changelog", "",
             "Changes between consecutive builds of the EVE Online Static Data Export, generated from "
             "`deltas/*/manifest.json`.",
             "It lists tables and counts only; the values are in `deltas/<build>/delta.jsonl.gz`.", ""]
    for m in reversed(manifests):
        t, v = m["totals"], m["verification"]
        lines.append(f"## Build {m['build']} ({str(m['releaseDate'])[:10]}) - base {m['lastBuild']}")
        lines.append("")
        lines.append(f"- {t['tablesChanged']} tables changed: +{t['added']} added, -{t['removed']} removed, "
                     f"~{t['changed']} modified, {t['schemaOps']} schema operations")
        if v["status"] != "ok":
            lines.append(f"- **Verification: {v['status']}** ({v['errors']} mismatches with CCP's changelog, "
                         f"changelog coverage {v['changelogCoverage']})")
        for name, e in sorted(m["tables"].items()):
            ops = ", ".join(f"{k} x{n}" for k, n in sorted(e.get("schemaOps", {}).items()))
            extra = (f" [renamed from {e['renamedFrom']}]" if e.get("renamedFrom") else "") + \
                    (f" [schema: {ops}]" if ops else "")
            lines.append(f"  - `{name}`: +{e['added']} -{e['removed']} ~{e['changed']}{extra}")
        lines.append("")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--deltas-dir", default="deltas")
    ap.add_argument("--work-dir", default="work", help="cache de changelogs y zips (no se publica)")
    ap.add_argument("--changelog", default="CHANGELOG.md")
    ap.add_argument("--max-builds", type=int, default=0, help="maximo de builds a procesar en esta ejecucion (0 = todos)")
    ap.add_argument("--first-build", type=int, default=FIRST_BUILD)
    ap.add_argument("--build", type=int, help="procesa solo este build (lo reprocesa si ya existe)")
    ap.add_argument("--changelog-only", action="store_true",
                    help="compara solo las tablas que lista el changelog (por defecto se comparan todas)")
    ap.add_argument("--dry-run", action="store_true", help="solo lista los builds pendientes")
    ap.add_argument("--keep-zips", action="store_true", help="no borra los zips que se descargaron")
    args = ap.parse_args()

    os.makedirs(os.path.join(args.work_dir, "changes"), exist_ok=True)
    os.makedirs(args.deltas_dir, exist_ok=True)
    changes_dir = os.path.join(args.work_dir, "changes")
    try:
        if args.build:
            meta = changelog_meta(args.build, changes_dir)
            if meta is None:
                raise s.DeltaError(f"no existe changes/{args.build}.jsonl")
            pending = [(args.build, meta["lastBuildNumber"])]
        else:
            pending = pending_builds(latest_build(), processed_builds(args.deltas_dir), args.first_build, changes_dir)
    except s.DeltaError as e:
        sys.exit(f"ERROR: {e}")

    if args.max_builds:
        pending = pending[:args.max_builds]
    print(f"builds pendientes: {[b for b, _ in pending] or 'ninguno'}")
    if args.dry_run or not pending:
        return

    zips = os.path.join(args.work_dir, "zips")
    created, failed = set(), None
    for build, last in pending:
        before = set(os.listdir(zips)) if os.path.isdir(zips) else set()
        print(f"\n=== build {build} (base {last})")
        try:
            meta = changelog_meta(build, changes_dir)
            m = process_build(build, last, args, meta.get("releaseDate"))
            print(f"-> deltas/{build}: {m['totals']['tablesChanged']} tablas, verificacion {m['verification']['status']}")
        except s.DeltaError as e:
            failed = f"build {build}: {e}"
            break
        finally:
            if os.path.isdir(zips):
                created |= set(os.listdir(zips)) - before
        if not args.keep_zips:   # conserva el zip de este build para el siguiente par; borra los demas que bajamos
            keep = f"eve-online-static-data-{build}-jsonl.zip"
            for z in sorted(created - {keep}):
                os.remove(os.path.join(zips, z))
                created.discard(z)
    if not args.keep_zips:
        for z in created:
            os.remove(os.path.join(zips, z))

    manifests = load_manifests(args.deltas_dir)
    write_index(args.deltas_dir, manifests)
    write_changelog(args.changelog, manifests)
    if failed:
        sys.exit(f"ERROR: {failed}")


if __name__ == "__main__":
    main()
