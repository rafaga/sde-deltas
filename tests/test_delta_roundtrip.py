"""Ida y vuelta del formato V2: aplicar delta_lines(A -> B) sobre A debe reconstruir B.

Ejecutar desde la raiz del repo:  python -m unittest discover -s tests -v
Las pruebas con zips reales se saltan si no estan en sde_delta_out/zips.
"""
import io, json, os, re, sys, unittest, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import sde_delta as s  # noqa: E402


def diff(A, B):
    added, removed = set(B) - set(A), set(A) - set(B)
    changed = {i for i in set(A) & set(B) if A[i] != B[i]}
    return added, removed, changed


def with_idx(schema_path, idx):
    """Ruta de esquema (a.[].b) + indices de lista -> ruta concreta (a.[3].b)."""
    it = iter(idx)
    return re.sub(r"\.\[\]", lambda m: f".[{next(it)}]", schema_path)


def apply_schema(state, op):
    for flat_rec in state.values():
        for p in [p for p in flat_rec if s.schema_of(p) == op.get("from", op.get("path"))]:
            if op["kind"] == "drop_path":
                del flat_rec[p]
            elif op["kind"] == "rename_path":
                flat_rec[with_idx(op["to"], re.findall(r"\.\[(\d+)\]", p))] = flat_rec.pop(p)


def apply_delta(A, lines):
    """Aplicador de referencia: devuelve el estado aplanado de la tabla tras aplicar las lineas."""
    state = {k: s.flat(s.body(v)) for k, v in A.items()}
    for r in lines:
        if r["op"] == "schema":
            apply_schema(state, r)
            continue
        k = str(r["id"])
        if r["op"] == "added":
            state[k] = s.flat(r["record"])
        elif r["op"] == "removed":
            del state[k]
        else:
            for f in r["fields"]:
                if "new" in f:
                    state[k][f["path"]] = f["new"]
                else:
                    del state[k][f["path"]]
    return state


def roundtrip(t, A, B, renamed_from=None):
    lines = list(s.delta_lines(t, A, B, *diff(A, B), s.table_schema_ops(A, B, renamed_from)))
    # el delta debe sobrevivir a la serializacion JSONL sin perder informacion
    lines = [json.loads(json.dumps(l, ensure_ascii=False)) for l in lines]
    return lines, apply_delta(A, lines), {k: s.flat(s.body(v)) for k, v in B.items()}


def rec(key, **kw):
    return {"_key": key, **kw}


class SyntheticRoundtrip(unittest.TestCase):
    def check(self, A, B):
        lines, got, want = roundtrip("t", A, B)
        self.assertEqual(got, want)
        return lines

    def test_added_removed_changed(self):
        A = {"1": rec(1, name={"en": "a"}), "2": rec(2, name={"en": "b"})}
        B = {"2": rec(2, name={"en": "B"}), "3": rec(3, name={"en": "c"})}
        ops = {l["id"]: l["op"] for l in self.check(A, B)}
        self.assertEqual(ops, {1: "removed", 2: "changed", 3: "added"})

    def test_field_missing_only_in_some_records_travels_per_record(self):
        # 'opt' sigue existiendo en el esquema de B (registro 2), asi que no es un drop_path
        A = {"1": rec(1, x=1, opt=True), "2": rec(2, x=2, opt=False)}
        B = {"1": rec(1, x=1, extra="v"), "2": rec(2, x=2, opt=False, extra="w")}
        changed = [l for l in self.check(A, B) if l["op"] == "changed"]
        fields = {f["path"]: f for f in changed[0]["fields"]}
        self.assertEqual(fields["opt"], {"path": "opt", "old": True})      # sin 'new' = eliminado
        self.assertEqual(fields["extra"], {"path": "extra", "new": "v"})   # sin 'old' = nuevo

    def test_null_value_is_not_a_missing_field(self):
        A = {"1": rec(1, x=None)}
        B = {"1": rec(1, x=5)}
        self.assertEqual(self.check(A, B)[0]["fields"], [{"path": "x", "old": None, "new": 5}])

    def test_list_grows_and_shrinks(self):
        A = {"1": rec(1, l=[1, 2, 3]), "2": rec(2, l=[{"a": 1}])}
        B = {"1": rec(1, l=[1, 9]), "2": rec(2, l=[{"a": 1}, {"a": 2}])}
        self.check(A, B)

    def test_empty_list_changes(self):
        A = {"1": rec(1, l=[]), "2": rec(2, l=[7])}
        B = {"1": rec(1, l=[7]), "2": rec(2, l=[])}
        self.check(A, B)

    def test_values_are_not_truncated(self):
        long = "x" * 5000
        A = {"1": rec(1, d={"en": "short"})}
        B = {"1": rec(1, d={"en": long})}
        self.assertEqual(self.check(A, B)[0]["fields"][0]["new"], long)

    def test_id_keeps_original_type(self):
        A = {}
        B = {"7": rec(7, x=1), "abc-uuid": rec("abc-uuid", x=2)}
        ids = {l["id"] for l in self.check(A, B)}
        self.assertEqual(ids, {7, "abc-uuid"})

    def test_record_has_no_key_and_unicode_survives(self):
        A = {}
        B = {"1": rec(1, name={"ja": "地球", "ru": "Земля"})}
        line = self.check(A, B)[0]
        self.assertNotIn("_key", line["record"])
        self.assertEqual(line["record"]["name"]["ja"], "地球")

    def test_identical_tables_produce_no_lines(self):
        A = {"1": rec(1, x=[1, {"a": 2}])}
        self.assertEqual(self.check(A, dict(A)), [])


def schema_ops(lines):
    return [l for l in lines if l["op"] == "schema"]


class SchemaClassification(unittest.TestCase):
    def check(self, A, B, renamed_from=None):
        lines, got, want = roundtrip("t", A, B, renamed_from)
        self.assertEqual(got, want)
        return lines

    def test_string_resolution_is_a_rename_without_per_record_lines(self):
        A = {str(i): rec(i, nameID={"en": f"n{i}", "es": f"e{i}"}, x=i) for i in range(1, 4)}
        B = {str(i): rec(i, name={"en": f"n{i}", "es": f"e{i}"}, x=i) for i in range(1, 4)}
        lines = self.check(A, B)
        self.assertEqual({l["op"] for l in lines}, {"schema"})   # ningun 'changed': todo lo cubre el esquema
        ren = {(o["from"], o["to"]): o.get("reason") for o in schema_ops(lines) if o["kind"] == "rename_path"}
        self.assertEqual(ren, {("nameID.en", "name.en"): "string_resolution",
                               ("nameID.es", "name.es"): "string_resolution"})

    def test_adding_an_id_suffix_is_not_string_resolution(self):
        A = {"1": rec(1, effectCategory=3), "2": rec(2, effectCategory=5)}
        B = {"1": rec(1, effectCategoryID=3), "2": rec(2, effectCategoryID=5)}
        ops = schema_ops(self.check(A, B))
        self.assertEqual(ops, [{"table": "t", "op": "schema", "kind": "rename_path",
                                "from": "effectCategory", "to": "effectCategoryID"}])

    def test_language_removal_is_a_drop(self):
        A = {"1": rec(1, name={"en": "a", "it": "i"}), "2": rec(2, name={"en": "b", "it": "j"})}
        B = {"1": rec(1, name={"en": "a"}), "2": rec(2, name={"en": "b"})}
        lines = self.check(A, B)
        self.assertEqual(lines, [{"table": "t", "op": "schema", "kind": "drop_path", "path": "name.it"}])

    def test_rename_inside_lists_keeps_positions(self):
        A = {"1": rec(1, planetAttributes=[{"k": 1}, {"k": 2}]), "2": rec(2, planetAttributes=[{"k": 3}])}
        B = {"1": rec(1, attributes=[{"k": 1}, {"k": 2}]), "2": rec(2, attributes=[{"k": 3}])}
        lines = self.check(A, B)
        self.assertEqual([(o["from"], o["to"]) for o in schema_ops(lines)],
                         [("planetAttributes.[].k", "attributes.[].k")])
        self.assertEqual(len(lines), 1)

    def test_rename_with_changed_values_falls_back_to_fields(self):
        A = {"1": rec(1, oldName="a"), "2": rec(2, oldName="b")}
        B = {"1": rec(1, newName="a"), "2": rec(2, newName="CHANGED")}
        lines = self.check(A, B)
        self.assertFalse([o for o in schema_ops(lines) if o["kind"] == "rename_path"])

    def test_constant_columns_with_different_names_are_not_a_rename(self):
        A = {"1": rec(1, gone=0), "2": rec(2, gone=0)}
        B = {"1": rec(1, fresh=0), "2": rec(2, fresh=0)}
        kinds = sorted(o["kind"] for o in schema_ops(self.check(A, B)))
        self.assertEqual(kinds, ["add_path", "drop_path"])

    def test_added_field_is_informative_and_values_travel_in_fields(self):
        A = {"1": rec(1, x=1), "2": rec(2, x=2)}
        B = {"1": rec(1, x=1, flag=True), "2": rec(2, x=2, flag=False)}
        lines = self.check(A, B)
        self.assertEqual([o["kind"] for o in schema_ops(lines)], ["add_path"])
        self.assertEqual(len([l for l in lines if l["op"] == "changed"]), 2)

    def test_changes_unrelated_to_schema_still_travel_as_fields(self):
        A = {"1": rec(1, nameID={"en": "a"}, v=1)}
        B = {"1": rec(1, name={"en": "a"}, v=2)}
        lines = self.check(A, B)
        self.assertEqual([l["fields"] for l in lines if l["op"] == "changed"],
                         [[{"path": "v", "old": 1, "new": 2}]])

    def test_removed_and_added_records_alongside_a_rename(self):
        A = {"1": rec(1, nameID={"en": "a"}), "2": rec(2, nameID={"en": "b"})}
        B = {"2": rec(2, name={"en": "b"}), "3": rec(3, name={"en": "c"})}
        self.check(A, B)

    def test_table_rename_detected_by_id_overlap(self):
        def make(tables):
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                for name, recs in tables.items():
                    z.writestr(f"{name}.jsonl", "".join(json.dumps(r) + "\n" for r in recs))
            return zipfile.ZipFile(buf)

        za = make({"graphicIDs": [rec(i, f="a") for i in range(10)], "same": [rec(1, x=1)]})
        zb = make({"graphics": [rec(i, f="a") for i in range(10)] + [rec(99, f="z")], "same": [rec(1, x=1)]})
        renamed = s.detect_table_renames(za, s.table_names(za), zb, s.table_names(zb))
        self.assertEqual(renamed, {"graphics": "graphicIDs"})
        A = s.load_table(za, "graphicIDs.jsonl")
        B = s.load_table(zb, "graphics.jsonl")
        lines = self.check(A, B, renamed_from="graphicIDs")
        self.assertEqual(schema_ops(lines)[0], {"table": "t", "op": "schema", "kind": "rename_table",
                                                "from": "graphicIDs"})
        self.assertEqual([l["op"] for l in lines if l["op"] != "schema"], ["added"])   # solo el ID 99

    def test_unrelated_tables_are_not_renames(self):
        za = zipfile.ZipFile(io.BytesIO(), "w")
        self.assertEqual(s.detect_table_renames(za, {}, za, {}), {})


ZIPS = os.path.join(ROOT, "sde_delta_out", "zips")
RANGES = [(3561556, 3569502), (3464040, 3466501),
          (2960198, 3030547), (3030547, 3031812), (3031812, 3041737)]   # los tres ultimos: renombres reales


def zip_path(build):
    return os.path.join(ZIPS, f"eve-online-static-data-{build}-jsonl.zip")


class RealZipRoundtrip(unittest.TestCase):
    def test_ranges(self):
        for a, b in RANGES:
            if not (os.path.exists(zip_path(a)) and os.path.exists(zip_path(b))):
                continue   # sin los zips de este rango no se prueba (no es un fallo)
            za, zb = zipfile.ZipFile(zip_path(a)), zipfile.ZipFile(zip_path(b))
            na, nb = s.table_names(za), s.table_names(zb)
            renamed = s.detect_table_renames(za, na, zb, nb)   # {nuevo: viejo}, como en main()
            tables = (set(na) | set(nb)) - set(renamed.values()) - {"_sde"}
            for t in sorted(tables):
                src = renamed.get(t, t)
                A = s.load_table(za, na[src]) if src in na else {}
                B = s.load_table(zb, nb[t]) if t in nb else {}
                with self.subTest(range=f"{a}->{b}", table=t):
                    _, got, want = roundtrip(t, A, B, src if src != t else None)
                    self.assertEqual(got, want)


if __name__ == "__main__":
    unittest.main()
