#!/usr/bin/env python3
"""Genera el delta entre dos builds del SDE de EVE usando los changelogs oficiales
como indice y los zips completos como fuente de valores.

Uso:
  python sde_delta.py A B [opciones]

  A, B              build inicial y build final (A < B), p.ej. 3561556 3569502

Opciones:
  --out DIR           directorio de salida (por defecto ./sde_delta_out)
  --changes-dir DIR   cache de changes/*.jsonl (por defecto sde_changelog_out/changes si existe)
  --variant jsonl     variante del zip (jsonl o yaml; solo jsonl esta soportado)
  --verify-all        compara TODAS las tablas, no solo las que lista el changelog
                      (detecta cambios no documentados; mas lento)
  --gzip              escribe delta.jsonl.gz (gzip determinista) en vez de delta.jsonl
  --measure           compara el peso (crudo y gzip, por tabla) del formato V2 con V1 (registro completo
                      tambien en cambios). Escribe measure.csv

Salida en DIR/delta_A_B/:
  summary.csv        por tabla: IDs predichos por el changelog vs IDs reales
  verification.csv   discrepancias entre changelog y datos reales
  delta.jsonl        una linea por (tabla, id, op), formato V2:
                       schema  -> {"table","op":"schema","kind":K,...}  se aplica ANTES que lo demas, a todos
                                  los registros de A. K: rename_table (from), rename_path (from, to[, reason]),
                                  drop_path (path), add_path (path; informativa, los valores van en records/fields)
                       added   -> {"table","id","op":"added","record":{...}}  registro completo sin _key
                       removed -> {"table","id","op":"removed"}
                       changed -> {"table","id","op":"changed","fields":[{"path","old","new"}]}
                     'id' conserva el tipo original de _key. En 'fields' los valores son completos
                     (sin truncar); falta 'old' si el campo es nuevo y falta 'new' si se elimino.
                     Las rutas usan indices de lista (messages.[5].en); en 'schema' van con [] (a.[].b).
                     Lo que cubre una operacion de esquema no se repite por registro
  schema_delta.csv   cambios de esquema por tabla: field_added/field_removed/field_renamed/table_renamed

Supuestos NO verificados (el script los comprueba e imprime avisos):
  - dentro del zip hay un <tabla>.jsonl por tabla y cada linea trae "_key" como ID
  - los builds viejos siguen descargables
"""
import argparse, csv, gzip, hashlib, json, os, re, sys, time, urllib.request, urllib.error, zipfile, zlib
from collections import defaultdict

BASE = os.environ.get("SDE_BASE", "https://developers.eveonline.com/static-data/tranquility")
# CCP pide identificar al cliente: SDE_USER_AGENT debe llevar un contacto (p. ej. la URL del repo)
USER_AGENT = os.environ.get("SDE_USER_AGENT", "sde-deltas")
ID_OPS = ("added", "changed", "changedLocalization", "removed")


# ---------- red ----------
def fetch(url, dest=None, retries=3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=120) as r:
                if dest is None:
                    return r.read().decode("utf-8")
                tmp = dest + ".part"
                with open(tmp, "wb") as f:
                    while True:
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                os.replace(tmp, dest)
                return True
        except urllib.error.HTTPError as e:
            if e.code in (403, 404):
                return None
            time.sleep(2 * (i + 1))
        except Exception:
            time.sleep(2 * (i + 1))
    return None


def jsonl(text):
    return [json.loads(l) for l in text.splitlines() if l.strip()]


# ---------- 1. cadena de changelogs entre A y B ----------
def load_chain(a, b, changes_dir):
    files, cur, complete = [], b, True
    while cur != a:
        path = os.path.join(changes_dir, f"{cur}.jsonl")
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
        else:
            text = fetch(f"{BASE}/changes/{cur}.jsonl")
            if text is None:
                complete = False
                print(f"AVISO: no existe changes/{cur}.jsonl; el changelog no cubre {a}->{b}")
                break
            open(path, "w", encoding="utf-8").write(text)
        recs = jsonl(text)
        meta = next((r for r in recs if r.get("_key") == "_meta"), {})
        files.append((cur, recs))
        cur = meta.get("lastBuildNumber")
        if cur is None or cur < a:
            complete = False
            print(f"AVISO: {a} no es ancestro de {b} en la cadena de changelogs")
            break
    files.reverse()
    return files, complete


def merge_changelog(files):
    """Efecto neto por (tabla, id) y banderas por tabla."""
    state = {}                                  # (tabla, id) -> estado neto
    tflags = defaultdict(set)                   # tabla -> {schemaChanged, fileAdded, ...}
    for build, recs in files:
        for r in recs:
            t = r.get("_key")
            if t == "_meta":
                continue
            for op in ("schemaChanged", "fileAdded", "fileRemoved", "fileRenamed"):
                if r.get(op):
                    tflags[t].add(op)
            for op in ID_OPS:
                for i in r.get(op, []) or []:
                    k, s = (t, str(i)), state.get((t, str(i)))
                    if op == "added":
                        state[k] = "changed" if s == "removed" else "added"
                    elif op == "changed":
                        state[k] = "added" if s == "added" else "changed"
                    elif op == "changedLocalization":
                        state[k] = s if s in ("added", "changed") else "changedLocalization"
                    elif op == "removed":
                        state[k] = "gone" if s == "added" else "removed"
    return {k: v for k, v in state.items() if v != "gone"}, tflags


# ---------- 2. zips ----------
def get_zip(build, variant, zdir):
    os.makedirs(zdir, exist_ok=True)
    path = os.path.join(zdir, f"eve-online-static-data-{build}-{variant}.zip")
    if not os.path.exists(path):
        print(f"descargando build {build} ...")
        if fetch(f"{BASE}/eve-online-static-data-{build}-{variant}.zip", path) is None:
            raise DeltaError(f"no se pudo descargar el zip del build {build} "
                             f"(puede que los builds viejos no se conserven)")
    return zipfile.ZipFile(path)


def table_names(z):
    return {os.path.basename(n)[:-6]: n for n in z.namelist() if n.endswith(".jsonl")}


def load_table(z, member):
    out = {}
    with z.open(member) as f:
        for line in f:
            if line.strip():
                r = json.loads(line)
                out[str(r.get("_key"))] = r
    return out


# ---------- 3. comparacion ----------
def flat(v, prefix="", out=None, schema=False):
    """Aplana a rutas. schema=True normaliza indices de lista a [] (notacion del YAML)."""
    out = {} if out is None else out
    if isinstance(v, dict):
        for k, x in v.items():
            flat(x, f"{prefix}.{k}" if prefix else str(k), out, schema)
    elif isinstance(v, list):
        if not v:
            out[prefix] = v
        for i, x in enumerate(v):
            flat(x, f"{prefix}.[]" if schema else f"{prefix}.[{i}]", out, schema)
    else:
        out[prefix] = v
    return out


def body(rec):
    return {k: v for k, v in rec.items() if k != "_key"}


IDX = re.compile(r"\.\[\d+\]")
NO_SCHEMA = ([], frozenset(), frozenset())   # (ops, rutas que dejan de existir, rutas nuevas por renombre)


def schema_of(path):
    """Ruta concreta (messages.[5].en) -> ruta de esquema (messages.[].en)."""
    return IDX.sub(".[]", path)


def field_changes(old, new, skip_old=frozenset(), skip_new=frozenset()):
    """Cambios de campo entre dos registros. Falta 'old' = campo nuevo; falta 'new' = campo eliminado.
    skip_old/skip_new: rutas de esquema ya cubiertas por una operacion de esquema (se omiten)."""
    fa, fb = flat(body(old)), flat(body(new))
    out = []
    for p in sorted(set(fa) | set(fb)):
        if p in fa and p in fb and fa[p] == fb[p]:
            continue
        if p not in fb and schema_of(p) in skip_old:
            continue
        if p not in fa and schema_of(p) in skip_new:
            continue
        ch = {"path": p}
        if p in fa:
            ch["old"] = fa[p]
        if p in fb:
            ch["new"] = fb[p]
        out.append(ch)
    return out


def delta_lines(t, A, B, added, removed, changed, sch=NO_SCHEMA):
    """Lineas del delta V2 de una tabla. Primero las operaciones de esquema (se aplican a todos los
    registros de A antes de lo demas), luego altas con registro completo, bajas con ID y cambios como
    lista de {path, old, new} con valores completos. 'id' conserva el tipo original de _key."""
    ops, skip_old, skip_new = sch
    for op in ops:
        yield {"table": t, **op}
    for i in sorted(added):
        yield {"table": t, "id": B[i]["_key"], "op": "added", "record": body(B[i])}
    for i in sorted(removed):
        yield {"table": t, "id": A[i]["_key"], "op": "removed"}
    for i in sorted(changed):
        fields = field_changes(A[i], B[i], skip_old, skip_new)
        if fields:   # si todo el cambio lo explican las operaciones de esquema, no hay linea
            yield {"table": t, "id": B[i]["_key"], "op": "changed", "fields": fields}


# ---------- clasificacion de cambios de esquema ----------
def _leaf(sp):
    """Ultimo segmento que no es [] de una ruta de esquema, sin sufijo 'ID', en minusculas."""
    seg = [s for s in sp.split(".") if s != "[]"][-1]
    return seg[:-2].lower() if len(seg) > 2 and seg.lower().endswith("id") else seg.lower()


def _name_key(sp):
    """Ruta de esquema normalizada (segmentos sin sufijo 'ID', en minusculas)."""
    return ".".join(s[:-2].lower() if len(s) > 2 and s.lower().endswith("id") else s.lower()
                    for s in sp.split("."))


def column_hashes(tbl, ids, wanted):
    """Huella de la 'columna' de cada ruta de esquema en wanted, sobre los registros ids:
    {ruta: (sha1, es_constante)}. Incluye el indice de lista, para que un renombre conserve posiciones."""
    cols = {sp: {} for sp in wanted}
    for i in ids:
        for p, v in flat(body(tbl[i])).items():
            sp = schema_of(p)
            if sp in cols:
                idx = tuple(int(x) for x in re.findall(r"\.\[(\d+)\]", p))
                cols[sp].setdefault(i, []).append((idx, v))
    out = {}
    for sp, col in cols.items():
        if not col:
            continue
        h, values = hashlib.sha1(), set()
        for i in sorted(col):
            ents = sorted(col[i], key=lambda e: e[0])
            h.update(json.dumps([i, ents], ensure_ascii=False).encode("utf-8"))
            values.update(json.dumps(v, ensure_ascii=False) for _, v in ents)
        out[sp] = (h.hexdigest(), len(values) == 1)
    return out


def classify_schema(A, B, pa, pb):
    """Clasifica los cambios de esquema de una tabla presente en A y B.

    pa/pb: rutas de esquema (schema_paths) de A y B. Una ruta que desaparece y otra que aparece con la
    misma columna de valores en los registros comunes es un renombre (rename_path); la etiqueta
    'string_resolution' marca el patron nameID.<lang> -> name.<lang>. El resto son drop_path/add_path.
    Todo es sin perdida: aplicar las operaciones y luego los cambios reconstruye B exacto.
    Devuelve (ops, skip_old, skip_new); ver NO_SCHEMA."""
    removed, added = pa - pb, pb - pa
    if not removed and not added:
        return NO_SCHEMA
    common = set(A) & set(B)
    ha, hb = column_hashes(A, common, removed), column_hashes(B, common, added)
    groups = defaultdict(lambda: ([], []))
    for sp, (h, _) in ha.items():
        groups[h][0].append(sp)
    for sp, (h, _) in hb.items():
        groups[h][1].append(sp)
    renames = {}
    for h, (rs, ns) in groups.items():
        if not rs or not ns:
            continue
        if len(rs) == len(ns) == 1:
            pairs = [(rs[0], ns[0])]
        else:   # columnas identicas entre si: solo se empareja si el nombre normalizado es unico
            rk, nk = defaultdict(list), defaultdict(list)
            for r in rs:
                rk[_name_key(r)].append(r)
            for n in ns:
                nk[_name_key(n)].append(n)
            pairs = [(rk[k][0], nk[k][0]) for k in rk if len(rk[k]) == 1 and len(nk.get(k, [])) == 1]
        for r, n in pairs:
            if ha[r][1] and _leaf(r) != _leaf(n):
                continue   # columna constante (p. ej. todo 0): sin nombre parecido es coincidencia
            renames[r] = n
    ops = []
    for r, n in sorted(renames.items()):
        op = {"op": "schema", "kind": "rename_path", "from": r, "to": n}
        if _name_key(r) == _name_key(n) and len(r) > len(n):   # el origen es el que lleva 'ID': nameID -> name
            op["reason"] = "string_resolution"
        ops.append(op)
    ops += [{"op": "schema", "kind": "drop_path", "path": p} for p in sorted(removed - set(renames))]
    ops += [{"op": "schema", "kind": "add_path", "path": p} for p in sorted(added - set(renames.values()))]
    return ops, frozenset(removed), frozenset(renames.values())


def table_schema_ops(A, B, renamed_from=None):
    """Operaciones de esquema de una tabla (ver classify_schema); vacio si es una tabla que aparece o
    desaparece entera (todo es alta/baja). renamed_from: nombre anterior si la tabla fue renombrada."""
    if not A or not B:
        return NO_SCHEMA
    ops, skip_old, skip_new = classify_schema(A, B, schema_paths(A), schema_paths(B))
    if renamed_from:
        ops = [{"op": "schema", "kind": "rename_table", "from": renamed_from}] + ops
    return ops, skip_old, skip_new


def detect_table_renames(za, na, zb, nb, threshold=0.8):
    """Tablas que desaparecen en A y aparecen en B con (casi) los mismos IDs -> {nuevo: viejo}."""
    gone = sorted(set(na) - set(nb) - {"_sde"})
    new = sorted(set(nb) - set(na) - {"_sde"})
    if not gone or not new:
        return {}
    ids_a = {t: set(load_table(za, na[t])) for t in gone}
    ids_b = {t: set(load_table(zb, nb[t])) for t in new}
    cand = []
    for g in gone:
        for n in new:
            union = ids_a[g] | ids_b[n]
            if union and len(ids_a[g] & ids_b[n]) / len(union) >= threshold:
                cand.append((len(ids_a[g] & ids_b[n]) / len(union), g, n))
    renamed, used = {}, set()
    for _, g, n in sorted(cand, reverse=True):
        if g not in used and n not in renamed:
            renamed[n] = g
            used.add(g)
    return renamed


class Sizer:
    """Cuenta bytes crudos y comprimidos (gzip/deflate) de un flujo de lineas JSONL."""

    def __init__(self):
        self.raw, self.gz, self._z = 0, 0, zlib.compressobj(6, zlib.DEFLATED, 31)

    def add(self, obj):
        data = (json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        self.raw += len(data)
        self.gz += len(self._z.compress(data))

    def close(self):
        self.gz += len(self._z.flush())
        return self.raw, self.gz


def measure_table(t, A, B, added, removed, changed, sch=NO_SCHEMA):
    """Tamano del delta de una tabla en los formatos V1 (cambios = registro completo) y V2 (ver --measure)."""
    v1, v2 = Sizer(), Sizer()
    for line in delta_lines(t, A, B, added, removed, changed, sch):
        v2.add(line)
    for i in sorted(added):
        v1.add({"table": t, "id": B[i]["_key"], "op": "added", "record": body(B[i])})
    for i in sorted(removed):
        v1.add({"table": t, "id": A[i]["_key"], "op": "removed"})
    for i in sorted(changed):
        v1.add({"table": t, "id": B[i]["_key"], "op": "changed", "record": body(B[i])})
    return v1.close(), v2.close()


def schema_paths(tbl):
    paths = set()
    for r in tbl.values():
        paths.update(flat({k: v for k, v in r.items() if k != "_key"}, schema=True).keys())
    return paths


class DeltaError(Exception):
    """Error que impide generar el delta (descarga fallida, zip con estructura inesperada...)."""


class DeltaWriter:
    """Escribe delta.jsonl o delta.jsonl.gz. El gzip es determinista (sin nombre ni fecha en la
    cabecera): el mismo contenido da los mismos bytes, asi que re-ejecutar no produce commits espurios."""

    def __init__(self, out, gz):
        self.path = os.path.join(out, "delta.jsonl.gz" if gz else "delta.jsonl")
        self._raw = open(self.path, "wb")
        self.f = (gzip.GzipFile(filename="", mode="wb", fileobj=self._raw, compresslevel=9, mtime=0)
                  if gz else self._raw)
        self.lines = 0

    def write(self, line):
        self.f.write((json.dumps(line, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        self.lines += 1

    def close(self):
        self.f.close()
        if self.f is not self._raw:
            self._raw.close()


def run(a, b, out_root, changes_dir=None, variant="jsonl", verify_all=False, measure=False,
        gzip_delta=False, zips_dir=None, out_dir=None):
    """Genera el delta a -> b en out_dir (por defecto out_root/delta_a_b) y devuelve un dict con
    estadisticas: tablas (altas/bajas/cambios/operaciones de esquema), verificacion y archivos."""
    cd = changes_dir or ("sde_changelog_out/changes" if os.path.isdir("sde_changelog_out/changes")
                         else os.path.join(out_root, "changes"))
    os.makedirs(cd, exist_ok=True)
    files, complete = load_chain(a, b, cd)
    predicted, tflags = merge_changelog(files)
    print(f"changelogs: {len(files)} builds, cobertura {'completa' if complete else 'INCOMPLETA'}; "
          f"{len(predicted)} IDs con efecto neto, {len(tflags)} tablas mencionadas")

    zd = zips_dir or os.path.join(out_root, "zips")
    za, zb = get_zip(a, variant, zd), get_zip(b, variant, zd)
    ta_names, tb_names = table_names(za), table_names(zb)
    print(f"tablas en A: {len(ta_names)}  en B: {len(tb_names)}  (ejemplo: {sorted(tb_names)[:3]})")
    if not tb_names:
        raise DeltaError("no se encontraron <tabla>.jsonl dentro del zip; revisa la estructura")

    renamed = detect_table_renames(za, ta_names, zb, tb_names)   # {nombre nuevo: nombre viejo}
    for new, old in sorted(renamed.items()):
        print(f"tabla renombrada (mismos IDs): {old} -> {new}")

    listed = {t for t, _ in predicted} | set(tflags)
    tables = sorted(set(ta_names) | set(tb_names)) if (verify_all or not complete) else sorted(
        listed & (set(ta_names) | set(tb_names)))
    tables = sorted((set(tables) - set(renamed.values())) | set(renamed))  # el par renombrado es una sola tabla
    tables = [t for t in tables if t != "_sde"]  # metadatos del build, cambia siempre
    print(f"tablas a comparar: {len(tables)}")

    out = out_dir or os.path.join(out_root, f"delta_{a}_{b}")
    os.makedirs(out, exist_ok=True)
    delta = DeltaWriter(out, gzip_delta)
    summary, verif, sdelta, measures, stats = [], [], [], [], []

    for t in tables:
        src = renamed.get(t, t)   # nombre de la tabla en A
        A = load_table(za, ta_names[src]) if src in ta_names else {}
        B = load_table(zb, tb_names[t]) if t in tb_names else {}
        added, removed = set(B) - set(A), set(A) - set(B)
        changed = {i for i in set(A) & set(B) if A[i] != B[i]}
        pa, pb = schema_paths(A), schema_paths(B)
        sch = table_schema_ops(A, B, src if src != t else None)

        for line in delta_lines(t, A, B, added, removed, changed, sch):
            delta.write(line)

        if measure and (added or removed or changed or sch[0]):
            (r1, g1), (r2, g2) = measure_table(t, A, B, added, removed, changed, sch)
            measures.append((t, len(added), len(removed), len(changed), r1, g1, r2, g2))

        if A and B:
            kinds = {"rename_table": "table_renamed", "rename_path": "field_renamed",
                     "drop_path": "field_removed", "add_path": "field_added"}
            for op in sch[0]:
                what = {"rename_table": f"{op.get('from')} -> {t}",
                        "rename_path": f"{op.get('from')} -> {op.get('to')}"}.get(op["kind"], op.get("path"))
                sdelta.append((t, kinds[op["kind"]], what + (f" [{op['reason']}]" if "reason" in op else "")))
        else:   # tabla entera que aparece o desaparece: todos sus campos
            for p in sorted(pb - pa):
                sdelta.append((t, "field_added", p))
            for p in sorted(pa - pb):
                sdelta.append((t, "field_removed", p))

        # verificacion contra el changelog
        pred = defaultdict(set)
        for (tt, i), s in predicted.items():
            if tt == t:
                pred[s].add(i)
        flags = tflags.get(t, set())
        pred_add, pred_rem = pred["added"], pred["removed"]
        pred_chg = pred["changed"] | pred["changedLocalization"]
        schema_full = "schemaChanged" in flags
        # Tabla que aparece o desaparece dentro del rango: todo su contenido es alta/baja,
        # el changelog solo trae la bandera fileAdded/fileRemoved (sin lista de IDs).
        # Sus 'changed' intermedios no son comparables contra A, asi que se omiten.
        whole_table = (t not in ta_names) or (t not in tb_names)
        if not whole_table:
            for i in sorted(added - pred_add):
                verif.append((t, i, "added_en_datos_no_en_changelog", "error"))
            for i in sorted(pred_add - added):
                verif.append((t, i, "added_en_changelog_no_en_datos", "error"))
            for i in sorted(removed - pred_rem):
                verif.append((t, i, "removed_en_datos_no_en_changelog", "error"))
            for i in sorted(pred_rem - removed):
                verif.append((t, i, "removed_en_changelog_no_en_datos", "error"))
            if not schema_full:
                for i in sorted(changed - pred_chg - pred_add):
                    verif.append((t, i, "changed_en_datos_no_en_changelog", "error"))
            # el changelog dice 'changed' pero A y B son iguales: normal si el cambio
            # se revirtio dentro del rango (informativo, no es un error)
            for i in sorted(pred_chg - changed - removed):
                verif.append((t, i, "changed_en_changelog_sin_cambio_neto", "info"))
        if src != t:
            flags = flags | {f"renamedFrom:{src}"}
        summary.append((t, ";".join(sorted(flags)), len(pred_add), len(added), len(pred_rem), len(removed),
                        len(pred_chg), len(changed), len(pb - pa), len(pa - pb)))
        n_ren = sum(1 for op in sch[0] if op["kind"] in ("rename_path", "rename_table"))
        kinds_n = defaultdict(int)
        for op in sch[0]:
            kinds_n[op["kind"]] += 1
        stats.append({"table": t, "added": len(added), "removed": len(removed), "changed": len(changed),
                      "schema_ops": dict(sorted(kinds_n.items())), "flags": sorted(flags),
                      "renamed_from": src if src != t else None})
        print(f"  {t}: +{len(added)} -{len(removed)} ~{len(changed)}"
              + (f"  esquema: {len(sch[0])} ops ({n_ren} renombres)" if sch[0] else "")
              + (f"  [{';'.join(sorted(flags))}]" if flags else ""))
    delta.close()

    def wcsv(name, header, rows):
        with open(os.path.join(out, name), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f); w.writerow(header); w.writerows(rows)

    wcsv("summary.csv", ["table", "changelog_flags", "pred_added", "real_added", "pred_removed", "real_removed",
                         "pred_changed(+loc)", "real_changed", "fields_added", "fields_removed"], summary)
    wcsv("verification.csv", ["table", "id", "issue", "severity"], verif)
    wcsv("schema_delta.csv", ["table", "kind", "field_path"], sdelta)
    if measure:
        wcsv("measure.csv", ["table", "added", "removed", "changed", "v1_raw", "v1_gz", "v2_raw", "v2_gz"],
             sorted(measures, key=lambda m: -m[4]))
        tot = [sum(m[k] for m in measures) for k in range(4, 8)]
        print(f"\ntamano del delta {a}->{b} (bytes): V1 crudo {tot[0]:,} gzip {tot[1]:,} | "
              f"V2 crudo {tot[2]:,} gzip {tot[3]:,}")
        for m in sorted(measures, key=lambda m: -m[4])[:5]:
            print(f"  {m[0]}: V1 {m[4]:,} ({m[5]:,} gz)  V2 {m[6]:,} ({m[7]:,} gz)")
    n_err = sum(1 for v in verif if v[3] == "error")
    print(f"\nresumen: {len(tables)} tablas, {n_err} discrepancias (error), "
          f"{len(verif) - n_err} informativas, {len(sdelta)} cambios de campos")
    print("salida en", os.path.abspath(out))
    errors = [{"table": v[0], "id": v[1], "issue": v[2]} for v in verif if v[3] == "error"]
    return {"a": a, "b": b, "complete": complete, "out_dir": out, "delta_file": delta.path,
            "delta_lines": delta.lines, "tables_compared": len(tables), "tables": stats,
            "verification": {"errors": n_err, "info": len(verif) - n_err, "error_details": errors[:100]}}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("a", type=int); ap.add_argument("b", type=int)
    ap.add_argument("--out", default="sde_delta_out")
    ap.add_argument("--changes-dir"); ap.add_argument("--variant", default="jsonl")
    ap.add_argument("--verify-all", action="store_true")
    ap.add_argument("--measure", action="store_true")
    ap.add_argument("--gzip", action="store_true", help="escribe delta.jsonl.gz (determinista) en vez de delta.jsonl")
    args = ap.parse_args()
    if args.a >= args.b:
        sys.exit("A debe ser menor que B")
    try:
        run(args.a, args.b, args.out, args.changes_dir, args.variant, args.verify_all, args.measure, args.gzip)
    except DeltaError as e:
        sys.exit(f"ERROR: {e}")


if __name__ == "__main__":
    main()
