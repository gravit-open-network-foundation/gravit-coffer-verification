import struct, json
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
def b58(b):
    n = int.from_bytes(b, "big"); s = ""
    while n: n, r = divmod(n, 58); s = B58[r] + s
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + s
PRIM = {"u8":"<B","i8":"<b","u16":"<H","i16":"<h","u32":"<I","i32":"<i","u64":"<Q","i64":"<q","u128":None,"i128":None,"f32":"<f","f64":"<d"}
class Dec:
    def __init__(self, idl): self.types = {t["name"]: t for t in idl["types"]}
    def read(self, t, buf, o):
        if isinstance(t, str):
            if t in ("u128","i128"):
                v = int.from_bytes(buf[o:o+16], "little", signed=(t=="i128")); return v, o+16
            if t in PRIM: f = PRIM[t]; return struct.unpack_from(f, buf, o)[0], o+struct.calcsize(f)
            if t == "bool": return buf[o] != 0, o+1
            if t == "pubkey": return b58(buf[o:o+32]), o+32
            if t == "bytes":
                n = struct.unpack_from("<I", buf, o)[0]; return buf[o+4:o+4+n].hex(), o+4+n
            if t == "string":
                n = struct.unpack_from("<I", buf, o)[0]; return buf[o+4:o+4+n].decode(), o+4+n
            raise ValueError(t)
        if "array" in t:
            et, n = t["array"]; out = []
            if et == "u8": return buf[o:o+n].hex(), o+n
            for _ in range(n): v, o = self.read(et, buf, o); out.append(v)
            return out, o
        if "vec" in t:
            n = struct.unpack_from("<I", buf, o)[0]; o += 4; out = []
            for _ in range(n): v, o = self.read(t["vec"], buf, o); out.append(v)
            return out, o
        if "option" in t:
            if buf[o] == 0: return None, o+1
            return self.read(t["option"], buf, o+1)
        if "defined" in t:
            return self.struct(t["defined"]["name"], buf, o)
        raise ValueError(t)
    def struct(self, name, buf, o):
        ty = self.types[name]["type"]
        if ty["kind"] == "struct":
            out = {}
            for f in ty["fields"]: out[f["name"]], o = self.read(f["type"], buf, o)
            return out, o
        if ty["kind"] == "enum":
            i = buf[o]; v = ty["variants"][i]; o += 1
            if "fields" in v:
                vals = {}
                for k, f in enumerate(v["fields"]):
                    ft = f["type"] if isinstance(f, dict) and "type" in f else f
                    nm = f.get("name", str(k)) if isinstance(f, dict) else str(k)
                    vals[nm], o = self.read(ft, buf, o)
                return {v["name"]: vals}, o
            return v["name"], o
        raise ValueError(ty["kind"])
