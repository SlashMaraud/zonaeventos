#!/usr/bin/env python3
"""
Scraper SOLO para https://zonaeventoslive.infinityfreeapp.com/

Recoge TODOS los enlaces ACESTREAM de la web (sin filtrar canales ni
carpetas) y genera zona.m3u, con una carpeta (group-title) por cada
carpeta de la web. Cualquier otro tipo de enlace se ignora.

Como la web es una pagina con JavaScript (y la protege InfinityFree),
se abre con Playwright (un Chromium sin pantalla). Se leen los datos por
capas, de mas a menos fiable:
  0) Registros DNS TXT (base64 + gzip) que la propia pagina consulta en
     dns.google: ahi guarda la lista (indice con carpetas -> cada carpeta
     apunta a otro registro con sus canales).
  1) JSON de la lista (tipo .w3u: grupos con estaciones y enlaces),
     tanto el que descarga la pagina como el que lleve incrustado.
  2) Enlaces sueltos (acestream://, getstream?id=...) con su nombre.
  3) Lo que se vea en la pagina (enlaces, onclick, data-...).

Los enlaces se escriben para tu engine local de Ace Stream:
    http://127.0.0.1:6878/ace/getstream?id=ID

Escribe zona_debug.txt (sin fechas, para que solo cambie si cambia algo).
"""

import re
import sys
import json
import gzip
import zlib
import base64

# ============================================================
# CONFIGURACION
# ============================================================

SITE_URL = "https://zonaeventoslive.infinityfreeapp.com/"

# Engine de Ace Stream que reproducira los enlaces.
#   Movil con Ace Serve / engine local : "http://127.0.0.1:6878"
#   Vacio ("")                         : usa enlaces acestream://
ENGINE_URL = "http://127.0.0.1:6878"

OUTPUT_FILE = "zona.m3u"
DEBUG_FILE = "zona_debug.txt"
SCREENSHOT_FILE = "zona_debug.png"

ATTEMPTS = 3   # InfinityFree a veces pide recargar: se reintenta

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# ============================================================
# LOG
# ============================================================

LOG = []


def log(msg=""):
    print(msg, flush=True)
    LOG.append(str(msg))


def write_debug():
    try:
        with open(DEBUG_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(LOG)[:60000] + "\n")
    except Exception as error:
        print(f"No se pudo escribir {DEBUG_FILE}: {error}")


# ============================================================
# UTILIDADES
# ============================================================

ID_RE = re.compile(
    r"(?:[?&](id|content_id|infohash)=|acestream://\??(?:infohash=)?)"
    r"([a-fA-F0-9]{40})"
)
HASH40_RE = re.compile(r"(?<![a-fA-F0-9])[a-fA-F0-9]{40}(?![a-fA-F0-9])")
NAME_RE = re.compile(r'"(?:name|title)"\s*:\s*"((?:[^"\\]|\\.){1,200})"')

LIST_KEYS = (
    "groups", "stations", "channels", "items", "list", "folders",
    "subLinks", "links",
)
LINK_KEYS = (
    "url", "link", "stream", "src", "href",
    "hash", "id", "infohash", "content_id",
)


def valid_hash(value):
    return bool(re.fullmatch(r"[a-f0-9]{40}", str(value).strip().lower()))


def clean_name(name):
    name = str(name or "").strip()
    name = re.sub(r"[\r\n\t]+", " ", name)
    name = name.replace(",", " - ")
    name = re.sub(r"\s+", " ", name)
    return name.strip()


def decode_json_text(raw):
    try:
        return json.loads('"' + raw + '"')
    except Exception:
        return raw


def record_from_value(value, name, group):
    """Convierte un texto (enlace, hash...) en un registro, o None."""
    value = (value or "").strip()
    if not value:
        return None

    match = ID_RE.search(value)
    if match:
        kind = "infohash" if match.group(1) == "infohash" else "content_id"
        return {"name": name, "group": group, "hash": match.group(2).lower(),
                "kind": kind, "url": None}

    match = HASH40_RE.search(value)
    if match:
        return {"name": name, "group": group, "hash": match.group(0).lower(),
                "kind": "content_id", "url": None}

    return None


# ============================================================
# LECTURA DE LA LISTA (JSON tipo .w3u)
# ============================================================

def link_from(obj):
    for key in LINK_KEYS:
        value = obj.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        record = record_from_value(value, "", None)
        if record:
            if key == "infohash" and record["kind"] == "content_id":
                record["kind"] = "infohash"
            return record
    return None


def walk_groups(obj, out, group=None):
    """
    Recorre grupos y estaciones, recordando el nombre de la carpeta mas
    interna, y recoge solo los enlaces Acestream.
    """
    if isinstance(obj, dict):
        raw_name = obj.get("name") or obj.get("title")
        name = raw_name.strip() if isinstance(raw_name, str) else None
        has_children = any(isinstance(obj.get(k), list) for k in LIST_KEYS)

        link = link_from(obj)
        if link and not has_children:
            link["name"] = name or ""
            link["group"] = group
            out.append(link)
            return

        child_group = name if (has_children and name) else group
        for value in obj.values():
            if isinstance(value, (dict, list)):
                walk_groups(value, out, child_group)

    elif isinstance(obj, list):
        for value in obj:
            walk_groups(value, out, group)


def extract_embedded_json(text, out):
    """Busca JSON de lista incrustado en un script (var data = {...})."""
    decoder = json.JSONDecoder()
    position = 0
    attempts = 0
    found_any = False

    pattern = r'[\{\[]\s*\{?\s*"(?:name|title|groups|stations|author|url)"'
    for match in re.finditer(pattern, text):
        if match.start() < position:
            continue
        attempts += 1
        if attempts > 300:
            break
        try:
            obj, end = decoder.raw_decode(text, match.start())
        except Exception:
            continue

        before = len(out)
        walk_groups(obj, out)
        if len(out) > before:
            found_any = True
        position = end

    return found_any


def collect_from_text(text, structured, loose):
    """
    structured = entradas con carpeta (de un JSON de lista)
    loose      = enlaces sueltos con el nombre que se encontro cerca
    """
    if not text:
        return

    if text.lstrip()[:1] in ("{", "["):
        try:
            before = len(structured)
            walk_groups(json.loads(text), structured)
            if len(structured) > before:
                return
        except Exception:
            pass

    if extract_embedded_json(text, structured):
        return

    for match in ID_RE.finditer(text):
        chunk = text[max(0, match.start() - 300):match.start()]
        names = NAME_RE.findall(chunk)
        kind = "infohash" if match.group(1) == "infohash" else "content_id"
        loose.append({
            "name": decode_json_text(names[-1]) if names else "",
            "group": None,
            "hash": match.group(2).lower(),
            "kind": kind,
            "url": None,
        })


# ============================================================
# CAPA 0: REGISTROS DNS TXT (base64 + gzip) DE LA PROPIA WEB
# ============================================================

SUB_KEYS = ("subLinks", "items", "channels", "links", "stations")


def decode_txt_payload(data):
    """Decodifica el contenido de un registro TXT: base64 -> gzip -> JSON."""
    if not isinstance(data, str) or not data.strip():
        return None

    # Google junta los trozos de 255 caracteres; por si vienen entre comillas
    segments = re.findall(r'"((?:[^"\\]|\\.)*)"', data)
    text = "".join(segments) if segments else data
    text = re.sub(r"\s+", "", text)

    variants = (text, text.replace("-", "+").replace("_", "/"))
    for variant in variants:
        padded = variant + "=" * (-len(variant) % 4)
        try:
            raw = base64.b64decode(padded)
        except Exception:
            continue

        try:
            if raw[:2] == b"\x1f\x8b":
                raw = gzip.decompress(raw)
            elif raw[:1] == b"x":
                raw = zlib.decompress(raw)
        except Exception:
            continue

        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            continue

    try:  # por si el TXT ya es JSON plano
        return json.loads(data)
    except Exception:
        return None


def walk_dns(records):
    """
    records = {nombre_del_registro: lista_json}. Los registros que nadie
    referencia son la raiz; las carpetas ("type": "category") apuntan con
    "ref" a otro registro con sus canales.
    """
    referenced = set()

    def collect_refs(obj):
        if isinstance(obj, dict):
            ref = obj.get("ref")
            if isinstance(ref, str):
                referenced.add(ref.lower())
            for value in obj.values():
                collect_refs(value)
        elif isinstance(obj, list):
            for value in obj:
                collect_refs(value)

    for payload in records.values():
        collect_refs(payload)

    roots = sorted(label for label in records if label not in referenced)
    found = []

    def walk_items(items, group, stack):
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            return

        for item in items:
            if not isinstance(item, dict):
                continue

            raw_name = item.get("name") or item.get("title")
            name = raw_name.strip() if isinstance(raw_name, str) else ""

            ref = item.get("ref")
            if isinstance(ref, str):
                label = ref.lower()
                if label in records and label not in stack:
                    walk_items(records[label], name or group, stack | {label})
                continue

            for key in SUB_KEYS:
                if isinstance(item.get(key), list):
                    walk_items(item[key], name or group, stack)

            for key in ("url", "link", "stream", "src"):
                record = record_from_value(item.get(key), name, group)
                if record:
                    found.append(record)
                    break

    for label in roots:
        walk_items(records[label], None, {label})

    return found, len(roots)


def collect_from_dns(bodies):
    """Busca respuestas DNS-sobre-HTTPS con la lista y las decodifica."""
    records = {}
    seen = 0

    for url, body in bodies:
        if '"Answer"' not in body:
            continue
        try:
            data = json.loads(body)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        seen += 1

        question = (data.get("Question") or [{}])[0].get("name", "")
        label = question.rstrip(".").split(".")[0].lower()
        answers = [
            a.get("data", "") for a in (data.get("Answer") or [])
            if isinstance(a, dict) and a.get("type") == 16
        ]

        payload = None
        for answer in answers:
            payload = decode_txt_payload(answer)
            if payload is not None:
                break
        if payload is None and len(answers) > 1:
            payload = decode_txt_payload("".join(answers))

        if label and payload is not None:
            records[label] = payload

    info = f"Respuestas DNS: {seen} | registros decodificados: {len(records)}"
    if not records:
        return [], info

    found, roots = walk_dns(records)
    info += f" | registros raiz: {roots}"
    return found, info


# ============================================================
# LO QUE SE VE EN LA PAGINA (DOM)
# ============================================================

JS_FIND_LINKS = r"""
() => {
  const hashRe = /[a-f0-9]{40}/i;
  const linkRe = /^acestream:|getstream/i;
  const rows = [];

  // Nombre de la carpeta: el elemento hermano que dice "N items"
  function headerOf(el) {
    let cur = el.parentElement;
    for (let hops = 0; cur && hops < 14; hops++) {
      for (const child of Array.from(cur.children)) {
        if (child.contains(el)) continue;
        const t = (child.innerText || '').trim();
        if (t.length > 0 && t.length < 80 && /\d+\s*items?\b/i.test(t)) {
          const name = t.replace(/\d+\s*items?\b/ig, '')
                        .replace(/[^\p{L}\p{N}\s+.&-]/gu, '').trim();
          if (name) return name;
        }
      }
      cur = cur.parentElement;
    }
    return '';
  }

  for (const el of document.querySelectorAll('*')) {
    const values = Array.from(el.attributes).map(a => a.value);
    for (const node of el.childNodes) {
      if (node.nodeType === 3) values.push(node.textContent);
    }
    for (const raw of values) {
      const value = (raw || '').trim();
      if (!value || value.length > 600) continue;
      if (!hashRe.test(value) && !linkRe.test(value)) continue;
      const box = el.closest(
        'tr, li, article, .card, .result, [class*=item], [class*=row], [class*=channel]'
      ) || el.parentElement;
      rows.push({
        value: value,
        text: (box ? box.innerText : '') || '',
        group: headerOf(el),
      });
    }
  }
  return rows;
}
"""


def records_from_dom(rows):
    out = []
    for row in rows[:5000]:
        value = row.get("value") or ""
        lines = [
            line.strip() for line in (row.get("text") or "").split("\n")
            if line.strip()
            and value not in line
            and not re.fullmatch(r"\d+\s*items?", line.strip(), re.IGNORECASE)
        ]
        record = record_from_value(
            value, lines[0] if lines else "", row.get("group") or None
        )
        if record:
            out.append(record)
    return out


# ============================================================
# SCRAPING
# ============================================================

def open_and_read(page, bodies):
    """Abre la pagina, despliega las carpetas y devuelve (registros, capa)."""
    try:
        page.goto(SITE_URL, wait_until="networkidle", timeout=90000)
    except Exception as error:
        log(f"    Aviso al abrir la pagina: {error!r}")

    # InfinityFree puede mostrar un reto JS y recargar: esperar un poco
    page.wait_for_timeout(5000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass

    # Desplegar todas las carpetas
    try:
        page.get_by_text("desplegar", exact=False).first.click(timeout=5000)
        page.wait_for_timeout(1500)
    except Exception:
        log("    (no se encontro el boton 'desplegar')")

    try:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(1000)
    except Exception:
        pass

    try:
        log(f"    Titulo de la pagina: {page.title()!r}")
    except Exception:
        pass

    # Resumen estable (sin tamanos ni orden) para que el debug solo cambie
    # cuando cambia algo de verdad
    otras = sorted({u.split("?")[0][:100] for u, _ in bodies
                    if "dns.google" not in u and "dns-query" not in u})
    for url in otras[:15]:
        log(f"    Respuesta: {url}")

    dns_found, dns_info = collect_from_dns(bodies)
    log(f"    {dns_info}")
    if dns_found:
        return dns_found, "DNS TXT (registros de la web)"

    structured = []
    loose = []
    for url, body in bodies:
        collect_from_text(body, structured, loose)
    try:
        collect_from_text(page.content(), structured, loose)
    except Exception:
        pass

    if structured:
        return structured, "JSON de la lista"
    if loose:
        return loose, "enlaces sueltos"

    try:
        rows = page.evaluate(JS_FIND_LINKS)
        dom = records_from_dom(rows)
        if dom:
            return dom, "contenido de la pagina"
    except Exception as error:
        log(f"    (dom) {error!r}")

    return [], "ninguna"


def scrape():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("Playwright no esta instalado.")
        return []

    found = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            context = browser.new_context(
                user_agent=UA,
                locale="es-ES",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            bodies = []

            def on_response(resp):
                try:
                    kind = resp.request.resource_type
                    if kind not in ("document", "fetch", "xhr", "script", "other"):
                        return
                    body = resp.text()
                    if body and len(body) < 8000000:
                        bodies.append((resp.url, body))
                except Exception:
                    pass

            page.on("response", on_response)

            for attempt in range(1, ATTEMPTS + 1):
                log(f"Intento {attempt} de {ATTEMPTS}")
                bodies.clear()

                found, layer = open_and_read(page, bodies)
                log(f"    Capa usada: {layer} | enlaces: {len(found)}")

                if found:
                    break

                try:
                    text = page.inner_text("body")
                    log(f"    Texto de la pagina: {text[:300]!r}")
                except Exception:
                    pass
                page.wait_for_timeout(5000)

            try:
                page.screenshot(path=SCREENSHOT_FILE)
            except Exception:
                pass
        finally:
            browser.close()

    return found


# ============================================================
# GENERACION M3U
# ============================================================

def final_url(record):
    if record["hash"]:
        if ENGINE_URL:
            param = "infohash" if record["kind"] == "infohash" else "id"
            return f"{ENGINE_URL.rstrip('/')}/ace/getstream?{param}={record['hash']}"
        if record["kind"] == "infohash":
            return f"acestream://?infohash={record['hash']}"
        return f"acestream://{record['hash']}"
    return record["url"]


def write_m3u(found):
    if not found:
        log("\nERROR: no se ha encontrado ningun enlace.")
        log("No se sobrescribe la lista existente.")
        return

    entries = ["#EXTM3U"]
    seen = set()
    counts = {}
    total = 0

    for number, record in enumerate(found, start=1):
        group = clean_name(record.get("group")) or "SIN CARPETA"
        name = clean_name(record.get("name")) or f"Canal {number}"
        url = final_url(record)

        key = (name, url)
        if not url or key in seen:
            continue
        seen.add(key)

        counts[group] = counts.get(group, 0) + 1
        total += 1
        entries.append(f'#EXTINF:-1 group-title="{group}",{name}\n{url}')

    with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
        file.write("\n".join(entries) + "\n")

    log("\n====================================")
    log("LISTA M3U GENERADA CORRECTAMENTE")
    log(f"Enlaces unicos: {total}")
    for group, count in counts.items():
        log(f"  {group}: {count}")
    log(f"Archivo: {OUTPUT_FILE}")
    log("====================================")


if __name__ == "__main__":
    try:
        write_m3u(scrape())
    except Exception as error:
        log(f"ERROR inesperado: {error!r}")
    finally:
        write_debug()

    # Salir siempre bien para que el workflow pueda guardar el resultado
    sys.exit(0)
