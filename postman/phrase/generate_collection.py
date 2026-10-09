#!/usr/bin/env python3
"""Build the Phrase Platform Postman collection and environments from the OpenAPI specs.

Usage:  python3 generate_collection.py
Output: Phrase-Platform-APIs.postman_collection.json
        Phrase-EU.postman_environment.json
        Phrase-US.postman_environment.json

The collection has two top-level parts:
  * "API-Referenz"    – every operation of every spec, grouped by API and tag.
  * "Test-Szenarien"  – runnable test projects that chain requests through variables.
"""
import copy
import json
import re
import uuid
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SPECS = HERE / "specs"
METHODS = ("get", "post", "put", "patch", "delete")


def load(name):
    p = SPECS / name
    return yaml.safe_load(p.read_text()) if p.suffix == ".yaml" else json.loads(p.read_text())


def uid(seed):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "phrase-postman/" + seed))


# --------------------------------------------------------------------------------------
# JavaScript snippets
# --------------------------------------------------------------------------------------

COLLECTION_PRE = r"""
// ---- Collection pre-request -------------------------------------------------------
// Warn early when the credentials for the called API are missing.
const url = pm.request.url.toString();
const isStudio = url.includes('studio');
if (isStudio && !pm.variables.get('studioApiKey')) {
    console.warn('studioApiKey ist leer – Studio-Requests werden mit 401 antworten.');
}
if (!isStudio && !pm.variables.get('accessToken')) {
    console.warn('accessToken ist leer – Platform-Requests werden mit 401 antworten.');
}
""".strip()

COLLECTION_TEST = r"""
// ---- Collection tests (laufen nach JEDEM Request) ---------------------------------------
pm.test('[global] kein 5xx-Serverfehler', () => pm.expect(pm.response.code).to.be.below(500));
const maxMs = Number(pm.variables.get('maxResponseTimeMs') || 30000);
pm.test(`[global] Antwortzeit < ${maxMs} ms`, () => pm.expect(pm.response.responseTime).to.be.below(maxMs));
const actionId = pm.response.headers.get('X-ActionId');
if (actionId) { console.log('X-ActionId:', actionId); }
""".strip()


def js(s):
    return [line for line in s.strip("\n").split("\n")]


def t_status(*codes):
    codes_js = ", ".join(str(c) for c in codes)
    label = "/".join(str(c) for c in codes)
    return f"pm.test('Status {label}', () => pm.expect(pm.response.code).to.be.oneOf([{codes_js}]));"


def t_json_has(*props):
    lines = ["const body = pm.response.json();"]
    for p in props:
        lines.append(f"pm.test('Antwort enthält \"{p}\"', () => pm.expect(body).to.have.property('{p}'));")
    return "\n".join(lines)


def set_var(name, expr):
    return f"pm.collectionVariables.set('{name}', {expr});"


SKIP_IF_MISSING = r"""
// Request überspringen, wenn eine benötigte Variable fehlt (z. B. Connector nicht konfiguriert).
const __missing = [{names}].filter(n => !pm.variables.get(n));
if (__missing.length) {{
    console.warn('Übersprungen – Variable(n) fehlen: ' + __missing.join(', '));
    if (pm.execution && pm.execution.skipRequest) {{ pm.execution.skipRequest(); }}
}}
"""


def skip_if_missing(*names):
    return SKIP_IF_MISSING.format(names=", ".join(f"'{n}'" for n in names)).strip()


POLL_PRE = r"""
// Polling: ab dem 2. Versuch kurz warten.
const __pollKey = 'poll_' + pm.info.requestId;
if (Number(pm.collectionVariables.get(__pollKey) || 0) > 0) {
    const __ms = Number(pm.variables.get('pollIntervalMs') || 5000);
    setTimeout(() => {}, __ms);
}
""".strip()


def poll_test(done_expr, label, on_done=""):
    """Repeat the current request (collection runner) until done_expr is truthy."""
    return f"""
const __pollKey = 'poll_' + pm.info.requestId;
const __attempt = Number(pm.collectionVariables.get(__pollKey) || 0) + 1;
const __max = Number(pm.variables.get('pollMaxAttempts') || 30);
let __done = false;
try {{ __done = !!({done_expr}); }} catch (e) {{ __done = false; }}
const __next = (n) => (pm.execution && pm.execution.setNextRequest) ? pm.execution.setNextRequest(n) : postman.setNextRequest(n);
if (!__done && __attempt < __max) {{
    pm.collectionVariables.set(__pollKey, __attempt);
    console.log('{label}: noch nicht fertig – Versuch ' + __attempt + '/' + __max);
    __next(pm.info.requestName);
}} else {{
    pm.collectionVariables.unset(__pollKey);
    pm.test('{label} abgeschlossen (nach ' + __attempt + ' Versuch(en))', () => pm.expect(__done, 'Polling-Limit erreicht').to.be.true);
    {on_done}
}}
""".strip()


# --------------------------------------------------------------------------------------
# Postman building blocks
# --------------------------------------------------------------------------------------

def parse_url(raw, path_vars=None, query=None):
    """Build a Postman url object. `raw` is '{{base}}/a/:b/c' (no query string)."""
    host, _, rest = raw.partition("/")
    path = [seg for seg in rest.split("/") if seg != ""] if rest else []
    url = {"raw": raw, "host": [host], "path": path}
    if query:
        url["query"] = query
        enabled = [q for q in query if not q.get("disabled")]
        if enabled:
            url["raw"] = raw + "?" + "&".join(f"{q['key']}={q['value']}" for q in enabled)
    if path_vars:
        url["variable"] = path_vars
    return url


def q(key, value, desc=None, disabled=False):
    d = {"key": key, "value": "" if value is None else str(value)}
    if desc:
        d["description"] = desc
    if disabled:
        d["disabled"] = True
    return d


def h(key, value, desc=None, disabled=False):
    return q(key, value, desc, disabled)


def event(listen, code):
    return {"listen": listen, "script": {"type": "text/javascript", "exec": js(code)}}


def json_body(obj):
    return {
        "mode": "raw",
        "raw": json.dumps(obj, indent=2, ensure_ascii=False),
        "options": {"raw": {"language": "json"}},
    }


def request_item(name, method, raw_url, *, query=None, path_vars=None, headers=None, body=None,
                 auth=None, pre=None, tests=None, desc=None, responses=None):
    req = {"method": method.upper(), "header": headers or [], "url": parse_url(raw_url, path_vars, query)}
    if body is not None:
        req["body"] = body
    if auth is not None:
        req["auth"] = auth
    if desc:
        req["description"] = desc
    item = {"id": uid(name + method + raw_url + str(desc)[:40]), "name": name, "request": req, "response": responses or []}
    events = []
    if pre:
        events.append(event("prerequest", pre))
    if tests:
        events.append(event("test", tests))
    if events:
        item["event"] = events
    return item


def folder(name, items, desc=None, auth=None, pre=None, tests=None):
    f = {"name": name, "item": items}
    if desc:
        f["description"] = desc
    if auth is not None:
        f["auth"] = auth
    events = []
    if pre:
        events.append(event("prerequest", pre))
    if tests:
        events.append(event("test", tests))
    if events:
        f["event"] = events
    return f


BEARER = {"type": "bearer", "bearer": [{"key": "token", "value": "{{accessToken}}", "type": "string"}]}
STUDIO_AUTH = {"type": "apikey", "apikey": [
    {"key": "key", "value": "X-API-Key", "type": "string"},
    {"key": "value", "value": "{{studioApiKey}}", "type": "string"},
    {"key": "in", "value": "header", "type": "string"},
]}
NOAUTH = {"type": "noauth"}
BAD_BEARER = {"type": "bearer", "bearer": [{"key": "token", "value": "invalid-token-for-negative-test", "type": "string"}]}
BAD_STUDIO = {"type": "apikey", "apikey": [
    {"key": "key", "value": "X-API-Key", "type": "string"},
    {"key": "value", "value": "invalid-api-key", "type": "string"},
    {"key": "in", "value": "header", "type": "string"},
]}


# --------------------------------------------------------------------------------------
# OpenAPI -> example values
# --------------------------------------------------------------------------------------

class Spec:
    def __init__(self, doc):
        self.doc = doc

    def resolve(self, node):
        seen = 0
        while isinstance(node, dict) and "$ref" in node and seen < 20:
            ref = node["$ref"]
            target = self.doc
            for part in ref.lstrip("#/").split("/"):
                target = target[part]
            extra = {k: v for k, v in node.items() if k != "$ref"}
            node = {**target, **{k: v for k, v in extra.items() if k in ("description", "example")}}
            seen += 1
        return node

    def sample(self, schema, depth=0, name=None):
        schema = self.resolve(schema or {})
        if depth > 6:
            return None
        if "example" in schema and schema["example"] not in ("", None):
            return copy.deepcopy(schema["example"])
        if "examples" in schema and isinstance(schema["examples"], list) and schema["examples"]:
            return copy.deepcopy(schema["examples"][0])
        if "default" in schema and schema["default"] not in ("", None):
            return copy.deepcopy(schema["default"])
        if "enum" in schema and schema["enum"]:
            return schema["enum"][0]
        for key in ("allOf",):
            if key in schema:
                merged = {}
                for sub in schema[key]:
                    v = self.sample(sub, depth + 1)
                    if isinstance(v, dict):
                        merged.update(v)
                return merged
        for key in ("oneOf", "anyOf"):
            if key in schema and schema[key]:
                return self.sample(schema[key][0], depth + 1, name)
        typ = schema.get("type")
        if isinstance(typ, list):
            typ = next((t for t in typ if t != "null"), None)
        if typ == "object" or "properties" in schema:
            out = {}
            for pname, pschema in (schema.get("properties") or {}).items():
                if pname == "credentials":  # use stored credentials via connectorUuid
                    continue
                v = self.sample(pschema, depth + 1, pname)
                if v is not None:
                    out[pname] = v
            return out
        if typ == "array":
            v = self.sample(schema.get("items", {}), depth + 1, name)
            return [] if v is None else [v]
        if typ == "integer" or typ == "number":
            return schema.get("minimum", 0)
        if typ == "boolean":
            return False
        if typ == "string":
            fmt = schema.get("format")
            if fmt == "uuid":
                return "00000000-0000-0000-0000-000000000000"
            if fmt == "date-time":
                return "2026-01-01T00:00:00Z"
            if fmt == "date":
                return "2026-01-01"
            if fmt == "binary":
                return None
            return name or "string"
        return None


def first_example(content):
    if "example" in content:
        return content["example"]
    ex = content.get("examples")
    if isinstance(ex, dict) and ex:
        v = next(iter(ex.values()))
        return v.get("value") if isinstance(v, dict) else v
    return None


# Variable substitution for example bodies -----------------------------------------------

CONNECTOR_VAR = {
    "Contentful": "contentfulConnectorUuid",
    "Google Drive": "googleDriveConnectorUuid",
    "GitHub": "githubConnectorUuid",
    "Braze": "brazeConnectorUuid",
    "Optimizely": "optimizelyConnectorUuid",
    "Tridion Docs": "tridionConnectorUuid",
}


def substitute(obj, mapping):
    if isinstance(obj, dict):
        return {k: (mapping[k] if k in mapping and not isinstance(obj[k], (dict, list)) else substitute(v, mapping))
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute(v, mapping) for v in obj]
    return obj


# --------------------------------------------------------------------------------------
# API reference generation
# --------------------------------------------------------------------------------------

API_DEFS = [
    {
        "file": "control-hub.yaml", "name": "Control Hub (Content Groups)", "base": "controlHubUrl",
        "strip_prefix": "", "auth": None,
    },
    {
        "file": "style-guides.json", "name": "Style Guides & Rules", "base": "styleGuideUrl",
        "strip_prefix": "", "auth": None,
    },
    {
        "file": "quality-evaluator.json", "name": "Quality Evaluator", "base": "qualityEvaluatorUrl",
        "strip_prefix": "", "auth": None,
    },
    {
        "file": "studio.json", "name": "Studio", "base": "studioUrl",
        "strip_prefix": "", "auth": STUDIO_AUTH,
    },
    {
        "file": "connectors.yaml", "name": "Connectors (Bifrost)", "base": "connectorsUrl",
        "strip_prefix": "", "auth": None,
    },
]


def path_var_value(api, path, pname, method):
    """Map a path parameter to the collection variable that holds it."""
    if api == "Style Guides & Rules":
        if pname == "id":
            return "{{ruleId}}" if path.startswith("/api/v1/rules") else "{{styleGuideId}}"
        if pname == "jobId":
            return "{{updateJobId}}" if "update-jobs" in path else "{{createJobId}}"
        if pname == "entityId":
            return "{{tmsProjectUid}}"
    if api == "Studio" and pname == "id":
        return "{{projectId}}"
    if api == "Connectors (Bifrost)" and pname == "uid":
        return "{{fileUid}}"
    return "{{" + pname + "}}"


QUERY_VALUE_OVERRIDES = {
    "contentGroupId": "{{contentGroupId}}",
    "language": "{{styleGuideLanguage}}",
    "objectUid": "{{referenceObjectUid}}",
    "objectType": "{{referenceObjectType}}",
    "locale": "{{targetLocale}}",
    "tenant": "TMS",
}


def reference_tests(api, op_id, method, path, ok_codes):
    lines = [t_status(*ok_codes)]
    capture = {
        # Control Hub
        "createGroup": "if (pm.response.code === 201) { " + set_var("groupId", "pm.response.json().id") + " }",
        "createGroupReference": "if (pm.response.code === 201) { " + set_var("referenceId", "pm.response.json().id") + " }",
        "listGroups": "const c = (pm.response.json().content || []); const s = c.find(g => g.groupType === 'SYSTEM'); if (s) { " + set_var("systemGroupId", "s.id") + " }",
        # Style Guides
        "createStyleGuideV2": "if (pm.response.code === 202) { " + set_var("createJobId", "pm.response.json().jobId") + " }",
        "createStyleGuide": "if (pm.response.code === 202) { " + set_var("createJobId", "pm.response.json().jobId") + " }",
        "updateStyleGuideV2": "if (pm.response.code === 202) { " + set_var("updateJobId", "pm.response.json().jobId") + " }",
        "updateStyleGuide": "if (pm.response.code === 202) { " + set_var("updateJobId", "pm.response.json().jobId") + " }",
        "getCreateStyleGuideJobStatus": "const b = pm.response.json(); if (b.status === 'COMPLETED' && b.result) { " + set_var("styleGuideId", "b.result.id") + " if (b.result.versions && b.result.versions.length) { " + set_var("versionId", "b.result.versions[b.result.versions.length - 1].id") + " } }",
        "getStyleGuide": "const b = pm.response.json(); if (b.versions && b.versions.length) { " + set_var("versionId", "b.versions[b.versions.length - 1].id") + " }",
        "createRule": "if (pm.response.code === 201) { " + set_var("ruleId", "pm.response.json().id") + " }",
        # Studio
        "createProject": "if (pm.response.code === 200) { " + set_var("projectId", "pm.response.json().id") + " }",
        "getProjectDetails": "const b = pm.response.json(); if (b.firstRecordingId) { " + set_var("recordingId", "b.firstRecordingId") + " }",
        "getSubtitleProfiles": "const d = (pm.response.json().data || []); if (d.length) { " + set_var("subtitleProfileId", "d[0].id") + " }",
        # Connectors
        "/files/v1/sync/upload-file": "const b = pm.response.json(); if (b.uid) { " + set_var("fileUid", "b.uid") + set_var("storageId", "b.uid") + " }",
    }
    if op_id in capture or path in capture:
        lines.append(capture.get(path) or capture[op_id])
    elif api == "Connectors (Bifrost)" and "download" in path:
        lines.append("try { const b = pm.response.json(); if (b.storageId) { " + set_var("storageId", "b.storageId") + " } } catch (e) {}")
    return "\n".join(lines)


def build_reference(api_def):
    doc = load(api_def["file"])
    spec = Spec(doc)
    api = api_def["name"]
    base = "{{" + api_def["base"] + "}}"
    tag_desc = {t["name"]: t.get("description", "") for t in doc.get("tags", [])}
    tag_order = []
    for t in doc.get("tags", []):
        if t["name"] not in tag_order:
            tag_order.append(t["name"])
    by_tag = {}

    for path, path_item in doc["paths"].items():
        common_params = path_item.get("parameters", [])
        for method in METHODS:
            if method not in path_item:
                continue
            op = path_item[method]
            tag = (op.get("tags") or ["Allgemein"])[0]
            if tag not in tag_order:
                tag_order.append(tag)
            params = [spec.resolve(p) for p in common_params + op.get("parameters", [])]

            # URL ---------------------------------------------------------------------
            pm_path = re.sub(r"\{(\w+)\}", r":\1", path)
            raw = base + pm_path
            path_vars, query, headers = [], [], []
            for p in params:
                schema = spec.resolve(p.get("schema", {}))
                desc = (p.get("description") or "").strip()
                if p["in"] == "path":
                    path_vars.append({"key": p["name"], "value": path_var_value(api, path, p["name"], method),
                                      "description": desc})
                elif p["in"] == "query":
                    if p["name"] in QUERY_VALUE_OVERRIDES:
                        val = QUERY_VALUE_OVERRIDES[p["name"]]
                    elif "example" in p:
                        val = p["example"]
                    else:
                        val = spec.sample(schema, name=p["name"])
                    if isinstance(val, bool):
                        val = str(val).lower()
                    if p["name"] == "name" and api == "Style Guides & Rules":
                        val = "Postman Style Guide {{$timestamp}}"
                    query.append(q(p["name"], val, desc, disabled=not p.get("required", False)))
                elif p["in"] == "header":
                    name = p["name"]
                    if name.lower() in ("content-type", "content-length") and method != "put":
                        continue
                    val = {
                        "X-Webhook": "{{webhookUrl}}",
                        "X-ActionId": "{{$guid}}",
                        "X-ResponseType": "ID",
                        "X-Upload-Mode": "STORAGE",
                        "X-Stream-Token": "{{streamToken}}",
                        "Content-Type": "application/octet-stream",
                        "Content-Length": "",
                    }.get(name, p.get("example", ""))
                    if name == "Content-Length":
                        continue  # Postman berechnet die Länge selbst
                    headers.append(h(name, val, desc[:300], disabled=not p.get("required", False)))

            # Body ----------------------------------------------------------------------
            body = None
            rb = op.get("requestBody", {}).get("content", {})
            mapping = {
                "connectorUuid": "{{" + CONNECTOR_VAR.get(tag, "connectorUuid") + "}}",
                "storageId": "{{storageId}}",
                "contentGroupId": "{{contentGroupId}}",
            }
            if "application/json" in rb:
                content = rb["application/json"]
                example = first_example(content)
                if example is None:
                    example = spec.sample(content.get("schema", {}))
                example = substitute(example, mapping)
                example = JSON_BODY_OVERRIDES.get(op.get("operationId"), example)
                body = json_body(example)
            elif "multipart/form-data" in rb:
                content = rb["multipart/form-data"]
                schema = spec.resolve(content.get("schema", {}))
                ex = first_example(content) or {}
                ex = substitute(ex, mapping)
                form = []
                for pname, pschema in (schema.get("properties") or {}).items():
                    pschema = spec.resolve(pschema)
                    items = spec.resolve(pschema.get("items", {})) if pschema.get("type") == "array" else {}
                    is_file = pschema.get("format") == "binary" or items.get("format") == "binary" or pname == "file"
                    pdesc = (pschema.get("description") or "").strip()
                    if is_file:
                        src = MULTIPART_FILE_SRC.get(api, "sample-files/sample-upload.txt")
                        form.append({"key": pname, "type": "file", "src": src, "description": pdesc,
                                     **({"disabled": True} if pname == "files" else {})})
                    else:
                        val = ex.get(pname, spec.sample(pschema, name=pname))
                        if pname == "fileUrls":
                            val = ["{{studioSampleMediaUrl}}"]
                        if pname == "subtitleProfiles":
                            val = [{"subtitleProfileId": "{{subtitleProfileId}}", "languageCode": "{{studioSourceLanguage}}"}]
                        if pname == "sourceLanguage":
                            val = "{{studioSourceLanguage}}"
                        if isinstance(val, (dict, list)):
                            val = json.dumps(val, ensure_ascii=False)
                        elif isinstance(val, bool):
                            val = str(val).lower()
                        optional = pname not in schema.get("required", []) and pname in STUDIO_OPTIONAL_FORM
                        form.append({"key": pname, "value": "" if val is None else str(val), "type": "text",
                                     "description": pdesc[:400], **({"disabled": True} if optional else {})})
                body = {"mode": "formdata", "formdata": form}
            elif "application/octet-stream" in rb:
                body = {"mode": "file", "file": {"src": "sample-files/sample-upload.txt"}}

            # Saved example responses ----------------------------------------------------
            responses, ok_codes = [], []
            for code, resp in op.get("responses", {}).items():
                if not str(code).startswith("2") and str(code) != "307":
                    continue
                ok_codes.append(int(code))
                resp = spec.resolve(resp)
                for ct, c in (resp.get("content") or {}).items():
                    ex = first_example(c)
                    if ex is None:
                        continue
                    responses.append({
                        "name": f"{code} – {resp.get('description', '')[:60].strip()}",
                        "originalRequest": {"method": method.upper(), "header": [], "url": parse_url(raw)},
                        "status": resp.get("description", "")[:60],
                        "code": int(code),
                        "_postman_previewlanguage": "json",
                        "header": [{"key": "Content-Type", "value": ct}],
                        "body": json.dumps(ex, indent=2, ensure_ascii=False) if not isinstance(ex, str) else ex,
                    })
                    break
            if not ok_codes:
                ok_codes = [200]

            summary = op.get("summary") or op.get("operationId") or f"{method.upper()} {path}"
            name = summary + (" [deprecated]" if op.get("deprecated") else "")
            desc_parts = [f"`{method.upper()} {path}`", ""]
            if op.get("operationId"):
                desc_parts.append(f"**operationId:** `{op['operationId']}`\n")
            if op.get("deprecated"):
                desc_parts.append("> ⚠️ **Deprecated** – für neue Integrationen die v2-Variante verwenden.\n")
            desc_parts.append(op.get("description") or "")
            item = request_item(
                name, method, raw, query=query, path_vars=path_vars, headers=headers, body=body,
                tests=reference_tests(api, op.get("operationId"), method, path, ok_codes),
                desc="\n".join(desc_parts).strip(), responses=responses,
            )
            item["id"] = uid(api + method + path)
            # Connector tags get a sync/async sub-folder
            sub = None
            if api == "Connectors (Bifrost)":
                sub = "Async" if "/async/" in path else ("Sync" if "/sync/" in path else None)
            by_tag.setdefault(tag, {}).setdefault(sub, []).append(item)

    folders = []
    for tag in tag_order:
        if tag not in by_tag:
            continue
        groups = by_tag[tag]
        items = list(groups.get(None, []))
        for sub in ("Sync", "Async"):
            if sub in groups:
                items.append(folder(sub, groups[sub], desc=(
                    "Blockierende Aufrufe – Ergebnis kommt direkt in der Antwort." if sub == "Sync" else
                    "Antwortet sofort mit `{ requestId, webHookUrl }`; das Ergebnis wird an `X-Webhook` "
                    "(`{{webhookUrl}}`) gesendet.")))
        folders.append(folder(tag, items, desc=tag_desc.get(tag)))

    info = doc.get("info", {})
    desc = f"**{info.get('title')}** – Version {info.get('version')}\n\n" + (info.get("description") or "")
    servers = doc.get("servers") or []
    if servers:
        desc += "\n\n**Server:**\n" + "\n".join(f"- `{s['url']}` {s.get('description', '')}" for s in servers)
    return folder(api, folders, desc=desc, auth=api_def["auth"])


MULTIPART_FILE_SRC = {
    "Style Guides & Rules": "sample-files/styleguide-de-v1.md",
    "Studio": "sample-files/sample-audio.wav",
    "Connectors (Bifrost)": "sample-files/sample-upload.txt",
}
STUDIO_OPTIONAL_FORM = {"summary", "customAttributes", "insights", "glossary", "safeCommunications", "pronunciations"}

JSON_BODY_OVERRIDES = {
    # Control Hub
    "createGroup": {"name": "Postman Test Group {{$timestamp}}", "description": "Angelegt über die Postman-Collection"},
    "updateGroup": {"name": "Postman Test Group (umbenannt)", "description": "Aktualisiert über die Postman-Collection"},
    "createGroupReference": {"objectType": "{{referenceObjectType}}", "objectUid": "{{referenceObjectUid}}"},
    # Style Guides / Rules
    "deleteStyleGuides": {"ids": ["{{styleGuideId}}"]},
    "searchStyleGuides": {"pageNumber": 0, "pageSize": 20, "sortBy": "lastModifiedAt", "sortDirection": "DESC"},
    "createRule": {
        "rule": "Use the formal form of address ('Sie') and never the informal 'du'.",
        "allContentGroups": False, "contentGroupIds": ["{{contentGroupId}}"],
        "allLanguages": False, "languages": ["{{ruleLanguage}}"],
        "active": True, "aiCheckEnabled": True,
    },
    "updateRule": {"rule": "Always use the formal form of address ('Sie').", "forceUnlink": True},
    "bulkDeleteRules": {"ruleIds": ["{{ruleId}}"]},
    "bulkUpdateRules": {"ruleIds": ["{{ruleId}}"], "active": False},
    "listRules": {"pageNumber": 0, "pageSize": 20, "sortBy": "createdAt", "sortDirection": "DESC",
                  "contentGroupIds": ["{{contentGroupId}}"]},
    # Quality Evaluator
    "evaluateV3": {
        "contentGroupId": "{{contentGroupId}}",
        "segments": [{"id": "segment-1", "source": "Hello, world!", "target": "Hallo, Welt!"}],
        "sourceLocaleCode": "{{sourceLocale}}", "targetLocaleCode": "{{targetLocale}}",
    },
    # Studio
    "translateRecording": {"languages": ["{{studioTargetLanguage}}"]},
    "dubRecording": {"languages": ["{{studioTargetLanguage}}"]},
}


# --------------------------------------------------------------------------------------
# Test scenarios ("Test-Projekte")
# --------------------------------------------------------------------------------------

CH = "{{controlHubUrl}}/api/v1/public"
SG = "{{styleGuideUrl}}/api"
QE = "{{qualityEvaluatorUrl}}"
ST = "{{studioUrl}}/v1"
CO = "{{connectorsUrl}}"

R = request_item  # shorthand


def ch_create_group(name_suffix, var="groupId"):
    return R(
        f"Content Group anlegen ({name_suffix})", "POST", f"{CH}/groups",
        body=json_body({"name": f"Postman {name_suffix} {{{{$timestamp}}}}", "description": "Automatisierter Test – darf gelöscht werden"}),
        tests="\n".join([
            t_status(201),
            t_json_has("id", "name", "groupType", "createdAt", "updatedAt"),
            "pm.test('groupType ist USER', () => pm.expect(body.groupType).to.eql('USER'));",
            set_var(var, "body.id"),
        ]),
    )


def ch_delete_group(var="groupId"):
    return R(
        "Cleanup: Content Group löschen", "DELETE", f"{CH}/groups/{{{{{var}}}}}",
        pre=skip_if_missing(var),
        tests=t_status(204, 404) + f"\npm.collectionVariables.unset('{var}');",
    )


def scenario_control_hub():
    items = [
        R("Content Groups auflisten (inkl. SYSTEM)", "GET", f"{CH}/groups",
          query=[q("page", 0), q("size", 100), q("includeSystem", "true")],
          tests="\n".join([
              t_status(200),
              t_json_has("content", "totalElements", "totalPages", "pageSize", "pageNumber"),
              "pm.test('Genau eine SYSTEM-Gruppe', () => pm.expect(body.content.filter(g => g.groupType === 'SYSTEM').length).to.eql(1));",
              "const sys = body.content.find(g => g.groupType === 'SYSTEM'); if (sys) { " + set_var("systemGroupId", "sys.id") + " }",
          ])),
        R("SYSTEM-Gruppe über Alias abrufen", "GET", f"{CH}/groups/SYSTEM",
          tests=t_status(200) + "\n" + t_json_has("id") + "\npm.test('groupType SYSTEM', () => pm.expect(body.groupType).to.eql('SYSTEM'));"),
        R("Content Groups ohne SYSTEM auflisten", "GET", f"{CH}/groups",
          query=[q("page", 0), q("size", 100), q("includeSystem", "false")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Keine SYSTEM-Gruppe enthalten', () => pm.expect(body.content.some(g => g.groupType === 'SYSTEM')).to.be.false);"),
        ch_create_group("Lifecycle"),
        R("Content Group abrufen", "GET", f"{CH}/groups/{{{{groupId}}}}",
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('ID stimmt', () => pm.expect(body.id).to.eql(pm.collectionVariables.get('groupId')));"),
        R("Content Group aktualisieren", "PUT", f"{CH}/groups/{{{{groupId}}}}",
          body=json_body({"name": "Postman Lifecycle (umbenannt)", "description": "Beschreibung aktualisiert"}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Name aktualisiert', () => pm.expect(body.name).to.eql('Postman Lifecycle (umbenannt)'));\npm.test('updatedAt >= createdAt', () => pm.expect(new Date(body.updatedAt) >= new Date(body.createdAt)).to.be.true);"),
        R("Referenz verknüpfen", "POST", f"{CH}/groups/{{{{groupId}}}}/references",
          pre="if (!pm.collectionVariables.get('referenceObjectUid') && !pm.environment.get('referenceObjectUid')) { pm.collectionVariables.set('referenceObjectUid', pm.variables.replaceIn('{{$guid}}')); }",
          body=json_body({"objectType": "{{referenceObjectType}}", "objectUid": "{{referenceObjectUid}}"}),
          tests="\n".join([t_status(201), t_json_has("id", "groupId", "objectType", "objectUid", "referenceType"),
                           "pm.test('referenceType USER', () => pm.expect(body.referenceType).to.eql('USER'));",
                           set_var("referenceId", "body.id")])),
        R("Referenzen der Gruppe auflisten", "GET", f"{CH}/groups/{{{{groupId}}}}/references",
          query=[q("objectType", "{{referenceObjectType}}"), q("page", 0), q("size", 20), q("includeSystem", "false")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Referenz enthalten', () => pm.expect(body.content.map(r => r.id)).to.include(pm.collectionVariables.get('referenceId')));"),
        R("Reverse Lookup: Gruppen zu Objekt", "GET", f"{CH}/references/groups",
          query=[q("objectType", "{{referenceObjectType}}"), q("objectUid", "{{referenceObjectUid}}")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Gruppe gefunden', () => pm.expect(body.groups.map(g => g.id)).to.include(pm.collectionVariables.get('groupId')));"),
        R("Referenz über ID entfernen", "DELETE", f"{CH}/groups/{{{{groupId}}}}/references/{{{{referenceId}}}}",
          tests=t_status(204)),
        R("Referenz erneut verknüpfen", "POST", f"{CH}/groups/{{{{groupId}}}}/references",
          body=json_body({"objectType": "{{referenceObjectType}}", "objectUid": "{{referenceObjectUid}}"}),
          tests=t_status(201)),
        R("Referenz über Objekt-Identität entfernen", "DELETE", f"{CH}/groups/{{{{groupId}}}}/references",
          query=[q("objectType", "{{referenceObjectType}}"), q("objectUid", "{{referenceObjectUid}}")],
          tests=t_status(204)),
        R("Entfernte Referenz erneut löschen → 404", "DELETE", f"{CH}/groups/{{{{groupId}}}}/references",
          query=[q("objectType", "{{referenceObjectType}}"), q("objectUid", "{{referenceObjectUid}}")],
          tests=t_status(404)),
        R("Content Group löschen", "DELETE", f"{CH}/groups/{{{{groupId}}}}", tests=t_status(204)),
        R("Gelöschte Gruppe abrufen → 404", "GET", f"{CH}/groups/{{{{groupId}}}}",
          tests=t_status(404) + "\npm.collectionVariables.unset('groupId');"),
    ]
    return folder("01 · Control Hub – Content-Group-Lifecycle", items, desc=(
        "Vollständiger CRUD-Durchlauf für Content Groups und deren Objekt-Referenzen:\n"
        "Auflisten → Anlegen → Lesen → Ändern → Referenz verknüpfen/suchen/entfernen → Löschen → 404 prüfen.\n\n"
        "`referenceObjectType` (Default `PLATFORM_QA_CHECK`) und `referenceObjectUid` (wird automatisch "
        "generiert) steuern das verknüpfte Objekt."))


def scenario_control_hub_negative():
    items = [
        R("Unbekannte Gruppe → 404", "GET", f"{CH}/groups/does-not-exist-{{{{$guid}}}}", tests=t_status(404)),
        R("Gruppe ohne Namen anlegen → 400/422", "POST", f"{CH}/groups",
          body=json_body({"name": "", "description": "invalid"}), tests=t_status(400, 422)),
        R("Gruppe ohne Body-Feld 'name' → 400/422", "POST", f"{CH}/groups",
          body=json_body({"description": "missing name"}), tests=t_status(400, 422)),
        R("SYSTEM-Gruppe ändern → 403", "PUT", f"{CH}/groups/SYSTEM",
          body=json_body({"name": "hack"}), tests=t_status(403, 422)),
        R("SYSTEM-Gruppe löschen → 403", "DELETE", f"{CH}/groups/SYSTEM", tests=t_status(403, 422)),
        R("TMS_PROJECT mit SYSTEM verknüpfen → 422", "POST", f"{CH}/groups/SYSTEM/references",
          body=json_body({"objectType": "TMS_PROJECT", "objectUid": "{{$guid}}"}), tests=t_status(422)),
        R("Ungültiger objectType → 400", "GET", f"{CH}/references/groups",
          query=[q("objectType", "NOT_A_TYPE"), q("objectUid", "x")], tests=t_status(400)),
        R("Reverse Lookup ohne objectUid → 400", "GET", f"{CH}/references/groups",
          query=[q("objectType", "TMS_PROJECT")], tests=t_status(400)),
        R("Page size > 100 → 400", "GET", f"{CH}/groups", query=[q("size", 101)], tests=t_status(400)),
        ch_create_group("Exclusive"),
        R("TMS_PROJECT mit Gruppe A verknüpfen", "POST", f"{CH}/groups/{{{{groupId}}}}/references",
          pre="pm.collectionVariables.set('negTmsProjectUid', 'postman-' + pm.variables.replaceIn('{{$guid}}'));",
          body=json_body({"objectType": "TMS_PROJECT", "objectUid": "{{negTmsProjectUid}}"}), tests=t_status(201)),
        ch_create_group("Exclusive B", var="groupIdB"),
        R("Gleiches TMS_PROJECT mit Gruppe B → 422 (nur 1 Gruppe erlaubt)", "POST", f"{CH}/groups/{{{{groupIdB}}}}/references",
          body=json_body({"objectType": "TMS_PROJECT", "objectUid": "{{negTmsProjectUid}}"}), tests=t_status(422)),
        ch_delete_group("groupIdB"),
        ch_delete_group("groupId"),
        R("Ohne Token → 401", "GET", f"{CH}/groups", auth=NOAUTH, tests=t_status(401)),
        R("Ungültiges Token → 401", "GET", f"{CH}/groups", auth=BAD_BEARER, tests=t_status(401)),
    ]
    return folder("02 · Control Hub – Validierung & Negativtests", items, desc=(
        "Prüft Fehlerfälle: 400/401/403/404/422 inkl. der Regel, dass TMS-Projekte nur in genau einer "
        "Content Group sein dürfen und SYSTEM nicht verändert werden kann."))


def poll_create_job_request():
    return R("Create-Job pollen bis COMPLETED", "GET", f"{SG}/v1/styleguides/async/create-jobs/{{{{createJobId}}}}",
             pre=POLL_PRE,
             tests="\n".join([
                 t_status(200),
                 "const body = pm.response.json();",
                 "if (body.status === 'FAILED') { console.error('Job FAILED', JSON.stringify(body.error)); }",
                 "pm.test('Job nicht FAILED', () => pm.expect(body.status).to.not.eql('FAILED'));",
                 poll_test("body.status !== 'PROCESSING'", "Style-Guide-Create-Job",
                           on_done="if (body.result) { " + set_var("styleGuideId", "body.result.id")
                           + " if (body.result.versions && body.result.versions.length) { "
                           + set_var("versionId", "body.result.versions[body.result.versions.length - 1].id")
                           + " } pm.test('extractedRulesCount vorhanden', () => pm.expect(body.result).to.have.property('extractedRulesCount')); }"),
             ]))


def scenario_style_guide():
    items = [
        ch_create_group("StyleGuide", var="contentGroupId"),
        R("Style Guide (v2) aus Markdown anlegen", "POST", f"{SG}/v2/styleguides",
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "Postman SG {{$timestamp}}"),
                 q("description", "Automatisierter Test"), q("includeVersions", "true"),
                 q("contentGroupId", "{{contentGroupId}}")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/styleguide-de-v1.md"}]},
          tests=t_status(202) + "\n" + t_json_has("jobId") + "\n" + set_var("createJobId", "body.jobId")),
        poll_create_job_request(),
        R("Style Guide mit Versionen abrufen", "GET", f"{SG}/v1/styleguides/{{{{styleGuideId}}}}",
          query=[q("includeVersions", "true")],
          tests="\n".join([t_status(200), t_json_has("id", "name", "language", "versions", "contentGroup"),
                           "pm.test('Content Group verknüpft', () => pm.expect(body.contentGroup.id).to.eql(pm.collectionVariables.get('contentGroupId')));",
                           "pm.test('Genau 1 Version', () => pm.expect(body.versions.length).to.eql(1));",
                           set_var("versionId", "body.versions[body.versions.length - 1].id")])),
        R("Version-Metadaten abrufen", "GET", f"{SG}/v1/styleguides/{{{{styleGuideId}}}}/versions/{{{{versionId}}}}",
          tests=t_status(200) + "\n" + t_json_has("id", "versionNumber", "fileName", "fileSizeBytes")),
        R("Version-Datei (Markdown) herunterladen", "GET", f"{SG}/v1/styleguides/{{{{styleGuideId}}}}/versions/{{{{versionId}}}}/file",
          query=[q("tenant", "TMS"), q("event", "download", disabled=True)],
          tests=t_status(200) + "\npm.test('Markdown-Inhalt', () => pm.expect(pm.response.text()).to.include('#'));"),
        R("Aus Style Guide extrahierte Rules suchen", "POST", f"{SG}/v1/rules/search",
          body=json_body({"pageNumber": 0, "pageSize": 100, "contentGroupIds": ["{{contentGroupId}}"]}),
          tests=t_status(200) + "\nconst body = pm.response.json();\nconsole.log('Extrahierte Rules:', JSON.stringify(body).slice(0, 2000));"),
        R("Metadaten aktualisieren (synchron, 200)", "PUT", f"{SG}/v2/styleguides/{{{{styleGuideId}}}}",
          query=[q("name", "Postman SG umbenannt {{$timestamp}}"), q("description", "Nur Metadaten"),
                 q("contentGroupId", "{{contentGroupId}}"), q("includeVersions", "true")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Name geändert', () => pm.expect(body.name).to.include('umbenannt'));"),
        R("Neue Version hochladen (asynchron, 202)", "PUT", f"{SG}/v2/styleguides/{{{{styleGuideId}}}}",
          query=[q("name", "Postman SG v2 {{$timestamp}}"), q("versionChangeReason", "Neue Regeln ergänzt"),
                 q("contentGroupId", "{{contentGroupId}}"), q("includeVersions", "true")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/styleguide-de-v2.md"}]},
          tests=t_status(202) + "\n" + t_json_has("jobId") + "\n" + set_var("updateJobId", "body.jobId")),
        R("Update-Job pollen bis COMPLETED", "GET", f"{SG}/v1/styleguides/async/update-jobs/{{{{updateJobId}}}}",
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           "pm.test('Job nicht FAILED', () => pm.expect(body.status).to.not.eql('FAILED'));",
                           poll_test("body.status !== 'PROCESSING'", "Style-Guide-Update-Job")])),
        R("Jetzt 2 Versionen vorhanden", "GET", f"{SG}/v1/styleguides/{{{{styleGuideId}}}}",
          query=[q("includeVersions", "true")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('2 Versionen', () => pm.expect(body.versions.length).to.eql(2));"),
        R("Style Guides suchen (Name + Content Group)", "POST", f"{SG}/v1/styleguides/search",
          body=json_body({"name": "Postman SG", "contentGroupIds": ["{{contentGroupId}}"], "sortBy": "lastModifiedAt",
                          "sortDirection": "DESC", "pageNumber": 0, "pageSize": 20}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Style Guide gefunden', () => pm.expect(JSON.stringify(body)).to.include(pm.collectionVariables.get('styleGuideId')));"),
        R("Reverse Lookup in Control Hub (PLATFORM_STYLE_GUIDE)", "GET", f"{CH}/references/groups",
          query=[q("objectType", "PLATFORM_STYLE_GUIDE"), q("objectUid", "{{styleGuideId}}")],
          tests=t_status(200) + "\nconst body = pm.response.json();\nconsole.log('Gruppen des Style Guides:', body.groups.map(g => g.id));"),
        R("Rules als Markdown exportieren", "GET", f"{SG}/v1/rules/{{{{contentGroupId}}}}/export",
          tests=t_status(200) + "\npm.test('Markdown-Tabelle', () => pm.expect(pm.response.text()).to.match(/\\|\\s*Rule\\s*\\|/));"),
        R("Zweiten Style Guide gleicher Sprache/Gruppe → Job FAILED oder 409", "POST", f"{SG}/v2/styleguides",
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "Postman SG Duplicate {{$timestamp}}"),
                 q("contentGroupId", "{{contentGroupId}}")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/styleguide-de-v1.md"}]},
          tests=t_status(202, 409) + "\nif (pm.response.code === 202) { " + set_var("duplicateJobId", "pm.response.json().jobId") + " }"),
        R("Duplikat-Job → FAILED (DUPLICATE_CONTENT_GROUP_LANGUAGE)", "GET", f"{SG}/v1/styleguides/async/create-jobs/{{{{duplicateJobId}}}}",
          pre=skip_if_missing("duplicateJobId") + "\n" + POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           poll_test("body.status !== 'PROCESSING'", "Duplikat-Job",
                                     on_done="pm.test('Status FAILED', () => pm.expect(body.status).to.eql('FAILED')); "
                                             "pm.test('Fehlercode DUPLICATE_CONTENT_GROUP_LANGUAGE', () => pm.expect(JSON.stringify(body.error)).to.include('DUPLICATE_CONTENT_GROUP_LANGUAGE')); "
                                             "pm.collectionVariables.unset('duplicateJobId');")])),
        R("Cleanup: Style Guide löschen", "DELETE", f"{SG}/v1/styleguides",
          pre=skip_if_missing("styleGuideId"),
          body=json_body({"ids": ["{{styleGuideId}}"]}),
          tests=t_status(200) + "\nconst body = pm.response.json();\nconsole.log(JSON.stringify(body));"),
        R("Gelöschter Style Guide → 404", "GET", f"{SG}/v1/styleguides/{{{{styleGuideId}}}}",
          tests=t_status(404) + "\npm.collectionVariables.unset('styleGuideId'); pm.collectionVariables.unset('versionId');"),
        ch_delete_group("contentGroupId"),
    ]
    return folder("03 · Style Guides – Lifecycle mit Versionen & Async-Jobs", items, desc=(
        "Legt eine Content Group an, lädt einen Markdown-Style-Guide hoch (v2, asynchron), pollt den Job, "
        "liest Versionen/Datei, prüft die extrahierten Rules, lädt eine neue Version hoch, testet die "
        "Duplikat-Regel (1 Style Guide je Sprache und Content Group) und räumt wieder auf.\n\n"
        "**Hinweis:** Multipart-Uploads verwenden Dateien aus `sample-files/`. In Postman muss das "
        "Working Directory auf den Ordner `postman/phrase` zeigen (Settings → General → Working Directory), "
        "bei Newman `--working-dir postman/phrase`."))


def scenario_rules():
    create_rule_body = {
        "rule": "Use the formal form of address ('Sie') and never the informal 'du'.",
        "allContentGroups": False, "contentGroupIds": ["{{contentGroupId}}"],
        "allLanguages": False, "languages": ["{{ruleLanguage}}"], "active": True, "aiCheckEnabled": True,
    }
    items = [
        ch_create_group("Rules", var="contentGroupId"),
        R("Rule anlegen (aktiv + AI-Check)", "POST", f"{SG}/v1/rules", body=json_body(create_rule_body),
          tests="\n".join([t_status(201), t_json_has("id", "rule", "active", "aiCheckEnabled", "contentGroups"),
                           "pm.test('aktiv', () => pm.expect(body.active).to.be.true);",
                           "pm.test('AI-Check aktiv', () => pm.expect(body.aiCheckEnabled).to.be.true);",
                           "pm.test('nicht mit Style Guide verknüpft', () => pm.expect(body.styleGuide == null).to.be.true);",
                           set_var("ruleId", "body.id")])),
        R("Rule abrufen", "GET", f"{SG}/v1/rules/{{{{ruleId}}}}",
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('ID stimmt', () => pm.expect(body.id).to.eql(pm.collectionVariables.get('ruleId')));"),
        R("Rules der Content Group suchen", "POST", f"{SG}/v1/rules/search",
          body=json_body({"pageNumber": 0, "pageSize": 20, "sortBy": "createdAt", "sortDirection": "DESC",
                          "contentGroupIds": ["{{contentGroupId}}"], "languages": ["{{ruleLanguage}}"]}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Rule gefunden', () => pm.expect(JSON.stringify(body)).to.include(pm.collectionVariables.get('ruleId')));"),
        R("Rules filtern: nur aktive mit AI-Check", "POST", f"{SG}/v1/rules/search",
          body=json_body({"pageNumber": 0, "pageSize": 20, "contentGroupIds": ["{{contentGroupId}}"], "active": True, "aiCheckEnabled": True}),
          tests=t_status(200)),
        R("Rule-Text ändern (PATCH)", "PATCH", f"{SG}/v1/rules/{{{{ruleId}}}}",
          body=json_body({"rule": "Always address the reader formally ('Sie').", "forceUnlink": True}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Text geändert', () => pm.expect(body.rule).to.include('formally'));"),
        R("Rule deaktivieren (PATCH active=false)", "PATCH", f"{SG}/v1/rules/{{{{ruleId}}}}",
          body=json_body({"active": False}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('inaktiv', () => pm.expect(body.active).to.be.false);\npm.test('AI-Check kaskadiert aus', () => pm.expect(body.aiCheckEnabled).to.be.false);"),
        R("Bulk aktivieren", "PATCH", f"{SG}/v1/rules", body=json_body({"ruleIds": ["{{ruleId}}"], "active": True}),
          tests=t_status(200) + "\nconsole.log(pm.response.text());"),
        R("Bulk deaktivieren", "PATCH", f"{SG}/v1/rules", body=json_body({"ruleIds": ["{{ruleId}}"], "active": False}),
          tests=t_status(200)),
        R("Zweite Rule (alle Sprachen) anlegen", "POST", f"{SG}/v1/rules",
          body=json_body({"rule": "Do not translate the product name 'Phrase'.", "allContentGroups": False,
                          "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": True}),
          tests=t_status(201) + "\n" + set_var("ruleId2", "pm.response.json().id")),
        R("Rules als Markdown exportieren", "GET", f"{SG}/v1/rules/{{{{contentGroupId}}}}/export",
          tests=t_status(200) + "\npm.test('beide Rules enthalten', () => { const t = pm.response.text(); pm.expect(t).to.include('formally'); pm.expect(t).to.include('Phrase'); });"),
        R("'All Groups'-Export", "GET", f"{SG}/v1/rules/all-groups/export", tests=t_status(200)),
        R("Einzelne Rule löschen", "DELETE", f"{SG}/v1/rules/{{{{ruleId}}}}", tests=t_status(204)),
        R("Gelöschte Rule → 404", "GET", f"{SG}/v1/rules/{{{{ruleId}}}}", tests=t_status(404)),
        R("Bulk-Delete (inkl. unbekannter ID → Teil-Erfolg)", "DELETE", f"{SG}/v1/rules",
          body=json_body({"ruleIds": ["{{ruleId2}}", "{{ruleId}}"]}),
          tests=t_status(200) + "\nconsole.log(pm.response.text());\npm.collectionVariables.unset('ruleId'); pm.collectionVariables.unset('ruleId2');"),
        ch_delete_group("contentGroupId"),
    ]
    return folder("04 · Rules – CRUD, Bulk & Export", items, desc=(
        "Rules auf einer eigenen Content Group: anlegen, lesen, suchen, ändern, aktivieren/deaktivieren "
        "(einzeln & bulk), Markdown-Export, Einzel- und Bulk-Löschung."))


def scenario_rules_negative():
    items = [
        ch_create_group("RulesNeg", var="contentGroupId"),
        R("AI-Check ohne aktive Rule → 400", "POST", f"{SG}/v1/rules",
          body=json_body({"rule": "x", "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": True,
                          "active": False, "aiCheckEnabled": True}), tests=t_status(400)),
        R("allContentGroups=true + contentGroupIds → 400", "POST", f"{SG}/v1/rules",
          body=json_body({"rule": "x", "allContentGroups": True, "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": True}),
          tests=t_status(400)),
        R("allLanguages=false ohne languages → 400", "POST", f"{SG}/v1/rules",
          body=json_body({"rule": "x", "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": False}), tests=t_status(400)),
        R("SYSTEM-Gruppe + weitere Gruppe kombiniert → 400", "POST", f"{SG}/v1/rules",
          pre=skip_if_missing("systemGroupId"),
          body=json_body({"rule": "x", "contentGroupIds": ["{{systemGroupId}}", "{{contentGroupId}}"], "allLanguages": True}),
          tests=t_status(400)),
        R("Rule-Text > 450 Zeichen → 400", "POST", f"{SG}/v1/rules",
          pre="pm.collectionVariables.set('longRuleText', 'x'.repeat(451));",
          body=json_body({"rule": "{{longRuleText}}", "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": True}),
          tests=t_status(400) + "\npm.collectionVariables.unset('longRuleText');"),
        R("Unbekannte Rule → 404", "GET", f"{SG}/v1/rules/01900000-0000-7000-8000-00000000dead", tests=t_status(404)),
        R("Ungültige UUID → 400", "GET", f"{SG}/v1/rules/not-a-uuid", tests=t_status(400, 404)),
        R("pageSize 101 → 400", "POST", f"{SG}/v1/rules/search", body=json_body({"pageNumber": 0, "pageSize": 101}),
          tests=t_status(400)),
        R("Ungültiges sortBy → 400", "POST", f"{SG}/v1/rules/search", body=json_body({"sortBy": "foo"}), tests=t_status(400)),
        R("Style Guide ohne Datei → 400", "POST", f"{SG}/v2/styleguides",
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "no file"), q("contentGroupId", "{{contentGroupId}}")],
          body={"mode": "formdata", "formdata": []}, tests=t_status(400, 415)),
        R("Style Guide mit falscher Dateiendung → 400", "POST", f"{SG}/v2/styleguides",
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "wrong ext"), q("contentGroupId", "{{contentGroupId}}")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/sample-upload.txt"}]},
          tests=t_status(400, 415)),
        R("Style Guide in SYSTEM-Gruppe → 409/Job FAILED", "POST", f"{SG}/v2/styleguides",
          pre=skip_if_missing("systemGroupId"),
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "system {{$timestamp}}"), q("contentGroupId", "{{systemGroupId}}")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/styleguide-de-v1.md"}]},
          tests=t_status(202, 400, 409)),
        R("Unbekannter Create-Job → 404", "GET", f"{SG}/v1/styleguides/async/create-jobs/{{{{$guid}}}}", tests=t_status(404)),
        R("Association ohne language/languageId → 400", "GET",
          f"{SG}/v1/styleguides/entities/{{{{tmsProjectUid}}}}/styleguide-associations",
          query=[q("tenant", "TMS")], tests=t_status(400)),
        R("Ohne Token → 401", "POST", f"{SG}/v1/rules/search", auth=NOAUTH, body=json_body({}), tests=t_status(401)),
        ch_delete_group("contentGroupId"),
    ]
    return folder("05 · Rules & Style Guides – Validierung & Negativtests", items, desc=(
        "Fehlerfälle aus der Spezifikation: Kombinationsregeln für `active`/`aiCheckEnabled`, "
        "`allContentGroups`/`allLanguages`, Längen- und Paging-Limits, Upload-Validierung, Auth.\n\n"
        "`systemGroupId` wird in Szenario 01 bzw. über *Content Groups auflisten* gesetzt."))


def scenario_quality_evaluator():
    eval_body = {
        "contentGroupId": "{{contentGroupId}}",
        "segments": [
            {"id": "formal", "source": "Can you help me?", "target": "Können Sie mir helfen?"},
            {"id": "informal", "source": "Can you help me?", "target": "Kannst du mir helfen?"},
            {"id": "empty", "source": "Thank you!", "target": ""},
        ],
        "sourceLocaleCode": "{{sourceLocale}}", "targetLocaleCode": "{{targetLocale}}",
    }
    items = [
        ch_create_group("QualityEval", var="contentGroupId"),
        R("AI-Check-Rule anlegen (formelle Anrede)", "POST", f"{SG}/v1/rules",
          body=json_body({"rule": "The translation must use the formal form of address ('Sie') and never the informal 'du'.",
                          "contentGroupIds": ["{{contentGroupId}}"], "allLanguages": False, "languages": ["{{ruleLanguage}}"],
                          "active": True, "aiCheckEnabled": True}),
          tests=t_status(201) + "\n" + set_var("ruleId", "pm.response.json().id")),
        R("Warten bis Eligibility-Check gelaufen ist", "GET", f"{SG}/v1/rules/{{{{ruleId}}}}",
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           poll_test("body.eligibilityCheckedAt", "Rule-Eligibility-Check",
                                     on_done="pm.test('Rule ist für AI-Checks geeignet', () => pm.expect(body.omittedReason == null, body.omittedReason).to.be.true);")])),
        R("Quality Profile der Content Group", "GET", f"{QE}/v3/qualityProfiles/{{{{contentGroupId}}}}",
          query=[q("locale", "{{targetLocale}}"), q("checkEnabled", "true"), q("checkText", "", disabled=True),
                 q("sort", "checkEnabled"), q("order", "desc")],
          tests="\n".join([t_status(200), t_json_has("rules"),
                           "pm.test('Rule im Profil', () => pm.expect(body.rules.map(r => r.id)).to.include(pm.collectionVariables.get('ruleId')));",
                           "pm.test('alle Rules mit AI-Check', () => body.rules.forEach(r => pm.expect(r.aiCheckEnabled).to.be.true));"])),
        R("Quality Profile mit Textfilter", "GET", f"{QE}/v3/qualityProfiles/{{{{contentGroupId}}}}",
          query=[q("checkText", "formal")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Filter greift', () => body.rules.forEach(r => pm.expect(r.text.toLowerCase()).to.include('formal')));"),
        R("Segmente evaluieren", "POST", f"{QE}/v3/evaluation", body=json_body(eval_body),
          tests="\n".join([
              t_status(200), t_json_has("segments"),
              "pm.test('3 Segmente zurück', () => pm.expect(body.segments.length).to.eql(3));",
              "const byId = Object.fromEntries(body.segments.map(s => [s.id, s]));",
              "pm.test('leeres Target wird nicht evaluiert', () => pm.expect(byId.empty.results).to.eql([]));",
              "pm.test('Ergebnisse je Rule', () => pm.expect(byId.formal.results.some(r => r.ruleUid === pm.collectionVariables.get('ruleId'))).to.be.true);",
              "pm.test('Ergebnis-Typ AI_CHECK', () => byId.formal.results.forEach(r => pm.expect(r.type).to.eql('AI_CHECK')));",
              "pm.test('[LLM] formelles Segment ist valide', () => pm.expect(byId.formal.results[0].evaluation.isValid).to.be.true);",
              "pm.test('[LLM] informelles Segment wird beanstandet', () => pm.expect(byId.informal.results[0].evaluation.isValid).to.be.false);",
              "console.log(JSON.stringify(body, null, 2));",
          ])),
        R("Doppelte Segment-IDs → 400", "POST", f"{QE}/v3/evaluation",
          body=json_body({**eval_body, "segments": [{"id": "dup", "source": "a", "target": "b"}, {"id": "dup", "source": "c", "target": "d"}]}),
          tests=t_status(400)),
        R("Keine Segmente → 400", "POST", f"{QE}/v3/evaluation", body=json_body({**eval_body, "segments": []}), tests=t_status(400)),
        R("Pflichtfeld targetLocaleCode fehlt → 400", "POST", f"{QE}/v3/evaluation",
          body=json_body({k: v for k, v in eval_body.items() if k != "targetLocaleCode"}), tests=t_status(400)),
        R("Rule deaktivieren", "PATCH", f"{SG}/v1/rules/{{{{ruleId}}}}", body=json_body({"active": False}), tests=t_status(200)),
        R("Ohne aktive Checks → leere results, kein Fehler", "POST", f"{QE}/v3/evaluation", body=json_body(eval_body),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('alle results leer', () => body.segments.forEach(s => pm.expect(s.results).to.eql([])));"),
        R("Analytics (letzte 30 Tage)", "GET", f"{QE}/v1/analytics",
          pre="const d = new Date(); const from = new Date(d.getTime() - 30*864e5);\npm.collectionVariables.set('analyticsFrom', from.toISOString().slice(0,10));\npm.collectionVariables.set('analyticsTo', d.toISOString().slice(0,10));",
          query=[q("dayAfter", "{{analyticsFrom}}"), q("dayBefore", "{{analyticsTo}}")],
          tests=t_status(200) + "\n" + t_json_has("items") + "\npm.test('Summenfelder vorhanden', () => body.items.forEach(i => ['day','sumProcessed','sumLocked','sumConfirmed','sumAiuConsumed'].forEach(k => pm.expect(i).to.have.property(k))));"),
        R("Evaluation ohne Token → 401", "POST", f"{QE}/v3/evaluation", auth=NOAUTH, body=json_body(eval_body), tests=t_status(401)),
        R("Cleanup: Rule löschen", "DELETE", f"{SG}/v1/rules/{{{{ruleId}}}}", pre=skip_if_missing("ruleId"),
          tests=t_status(204, 404) + "\npm.collectionVariables.unset('ruleId');"),
        ch_delete_group("contentGroupId"),
    ]
    return folder("06 · Quality Evaluator – AI-Checks End-to-End", items, desc=(
        "Content Group → AI-Check-Rule → Eligibility abwarten → Quality Profile prüfen → Segmente "
        "evaluieren (formell/informell/leer) → Validierungsfehler → Rule deaktivieren → Analytics → Cleanup.\n\n"
        "Tests mit Präfix **[LLM]** hängen von der Modellentscheidung ab und können vereinzelt schwanken. "
        "Evaluation und Analytics benötigen die IDM-Rolle **ADMIN** oder **OWNER**."))


def scenario_governance_e2e():
    items = [
        ch_create_group("Governance", var="contentGroupId"),
        R("Style Guide in Content Group hochladen", "POST", f"{SG}/v2/styleguides",
          query=[q("language", "{{styleGuideLanguage}}"), q("name", "Governance SG {{$timestamp}}"),
                 q("includeVersions", "true"), q("contentGroupId", "{{contentGroupId}}")],
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/styleguide-de-v1.md"}]},
          tests=t_status(202) + "\n" + set_var("createJobId", "pm.response.json().jobId")),
        poll_create_job_request(),
        R("Extrahierte Rules (mit Style-Guide-Link) prüfen", "POST", f"{SG}/v1/rules/search",
          body=json_body({"pageNumber": 0, "pageSize": 100, "contentGroupIds": ["{{contentGroupId}}"]}),
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           "const rules = body.content || body.items || [];",
                           "pm.test('mind. 1 Rule extrahiert', () => pm.expect(rules.length).to.be.above(0));",
                           "pm.test('Rules verweisen auf Style Guide', () => pm.expect(rules.some(r => r.styleGuide && r.styleGuide.id === pm.collectionVariables.get('styleGuideId'))).to.be.true);",
                           "if (rules.length) { " + set_var("ruleId", "rules[0].id") + " }"])),
        R("Verknüpfte Rule ändern ohne forceUnlink → 409", "PATCH", f"{SG}/v1/rules/{{{{ruleId}}}}",
          body=json_body({"rule": "Changed text without confirmation"}),
          tests=t_status(409) + "\npm.test('RULE_UNLINK_REQUIRES_CONFIRMATION', () => pm.expect(pm.response.text()).to.include('RULE_UNLINK_REQUIRES_CONFIRMATION'));"),
        R("Quality Profile spiegelt extrahierte Rules", "GET", f"{QE}/v3/qualityProfiles/{{{{contentGroupId}}}}",
          query=[q("locale", "{{targetLocale}}")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Rule im Profil', () => pm.expect(body.rules.map(r => r.id)).to.include(pm.collectionVariables.get('ruleId')));"),
        R("Übersetzung gegen Style Guide evaluieren", "POST", f"{QE}/v3/evaluation",
          body=json_body({"contentGroupId": "{{contentGroupId}}", "sourceLocaleCode": "{{sourceLocale}}", "targetLocaleCode": "{{targetLocale}}",
                          "segments": [
                              {"id": "1", "source": "Click Save to continue.", "target": "Klicken Sie auf „Speichern“, um fortzufahren."},
                              {"id": "2", "source": "Click Save to continue.", "target": "Klick auf Speichern um weiter zu machen!!"},
                          ]}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Ergebnisse vorhanden', () => pm.expect(body.segments[0].results.length).to.be.above(0));\nconsole.log(JSON.stringify(body, null, 2));"),
        R("Association für TMS-Projekt nachschlagen", "GET", f"{SG}/v1/styleguides/entities/{{{{tmsProjectUid}}}}/styleguide-associations",
          pre=skip_if_missing("tmsProjectUid"),
          query=[q("tenant", "TMS"), q("entityType", "PROJECT"), q("language", "{{styleGuideLanguage}}")],
          tests=t_status(200, 404)),
        R("Cleanup: Style Guide löschen", "DELETE", f"{SG}/v1/styleguides", pre=skip_if_missing("styleGuideId"),
          body=json_body({"ids": ["{{styleGuideId}}"]}),
          tests=t_status(200) + "\npm.collectionVariables.unset('styleGuideId'); pm.collectionVariables.unset('versionId');"),
        R("Cleanup: verbleibende Rules der Gruppe löschen", "POST", f"{SG}/v1/rules/search",
          body=json_body({"pageNumber": 0, "pageSize": 100, "contentGroupIds": ["{{contentGroupId}}"]}),
          tests=t_status(200) + "\nconst body = pm.response.json(); const rules = body.content || body.items || [];\n"
                "if (rules.length) {\n  pm.sendRequest({ url: pm.variables.replaceIn('{{styleGuideUrl}}/api/v1/rules'), method: 'DELETE',\n"
                "    header: { 'Authorization': 'Bearer ' + pm.variables.get('accessToken'), 'Content-Type': 'application/json' },\n"
                "    body: { mode: 'raw', raw: JSON.stringify({ ruleIds: rules.map(r => r.id) }) } },\n"
                "    (err, res) => pm.test('Bulk-Delete Rules ok', () => { pm.expect(err).to.be.null; pm.expect(res.code).to.eql(200); }));\n}\n"
                "pm.collectionVariables.unset('ruleId');"),
        ch_delete_group("contentGroupId"),
    ]
    return folder("07 · Cross-API – Governance-Flow (Control Hub → Style Guide → Rules → Quality Evaluator)", items, desc=(
        "Integrationstest über drei Services: Ein Style Guide wird einer Content Group zugeordnet, daraus "
        "extrahierte Rules erscheinen im Quality Profile und werden bei der Evaluation angewendet. Prüft "
        "außerdem den Unlink-Schutz (409) für aus Style Guides stammende Rules."))


def scenario_studio_resources():
    def listing(name, path, var=None):
        t = [t_status(200), t_json_has("data"), "pm.test('data ist Array', () => pm.expect(body.data).to.be.an('array'));"]
        if var:
            t.append(f"if (body.data.length) {{ {set_var(var, 'body.data[0].id')} }}")
        return R(name, "GET", f"{ST}/{path}", tests="\n".join(t))
    items = [
        listing("Subtitle Profiles", "subtitle-profiles", "subtitleProfileId"),
        R("Subtitle Profile vorhanden", "GET", f"{ST}/subtitle-profiles",
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('mind. ein aktives Profil', () => pm.expect(body.data.some(p => p.active)).to.be.true);"),
        listing("Pronunciations", "pronunciations", "pronunciationId"),
        listing("Glossaries", "glossaries", "glossaryId"),
        listing("Insights", "insights", "insightId"),
        listing("Safe Communications", "safe-communications", "safeCommunicationId"),
        R("Projekte: alle", "GET", f"{ST}/projects", query=[q("status", "ALL"), q("page", 1), q("pageSize", 50)],
          tests=t_status(200) + "\n" + t_json_has("data", "pagination") + "\npm.test('pageSize respektiert', () => pm.expect(body.data.length).to.be.at.most(50));"),
        R("Projekte: nur COMPLETED", "GET", f"{ST}/projects", query=[q("status", "COMPLETED"), q("pageSize", 20)],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('nur COMPLETED', () => body.data.forEach(p => pm.expect(p.status).to.eql('COMPLETED')));"),
        R("Projekte: Team-Projekte", "GET", f"{ST}/projects", query=[q("teamProjects", "true")], tests=t_status(200)),
        R("Projekte: Volltextsuche", "GET", f"{ST}/projects", query=[q("search", "Postman")], tests=t_status(200)),
        R("Ungültiger Status-Filter → 400", "GET", f"{ST}/projects", query=[q("status", "FOO")], tests=t_status(400)),
        R("pageSize 1001 → 400", "GET", f"{ST}/projects", query=[q("pageSize", 1001)], tests=t_status(400)),
        R("Ungültiger API-Key → 401", "GET", f"{ST}/projects", auth=BAD_STUDIO, tests=t_status(401)),
        R("Unbekanntes Projekt → 404", "GET", f"{ST}/projects/00000000000000000000000000", tests=t_status(400, 404)),
    ]
    return folder("08 · Studio – Ressourcen, Listen & Negativtests", items, auth=STUDIO_AUTH, desc=(
        "Liest alle Stammdaten (Subtitle Profiles, Pronunciations, Glossaries, Insights, Safe Communications) "
        "und speichert jeweils die erste ID für Szenario 09. Testet Filter/Paging und Fehlerfälle."))


def scenario_studio_pipeline():
    status_url = f"{ST}/recordings/{{{{recordingId}}}}/status"
    items = [
        R("Subtitle Profile ermitteln", "GET", f"{ST}/subtitle-profiles",
          tests=t_status(200) + "\nconst body = pm.response.json();\nconst p = body.data.find(x => x.active) || body.data[0];\npm.test('Profil gefunden', () => pm.expect(p).to.exist);\n" + set_var("subtitleProfileId", "p.id")),
        R("Projekt aus Datei-URL anlegen", "POST", f"{ST}/projects", query=[q("title", "Postman Pipeline {{$timestamp}}")],
          body={"mode": "formdata", "formdata": [
              {"key": "sourceLanguage", "value": "{{studioSourceLanguage}}", "type": "text"},
              {"key": "subtitleProfiles", "value": json.dumps([{"subtitleProfileId": "{{subtitleProfileId}}", "languageCode": "{{studioSourceLanguage}}"}]), "type": "text"},
              {"key": "summary", "value": "true", "type": "text"},
              {"key": "fileUrls", "value": json.dumps(["{{studioSampleMediaUrl}}"]), "type": "text"},
          ]},
          tests=t_status(200) + "\n" + t_json_has("id") + "\n" + set_var("projectId", "body.id")),
        R("Projekt-Details → recordingId", "GET", f"{ST}/projects/{{{{projectId}}}}",
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           poll_test("body.firstRecordingId", "Recording-Anlage",
                                     on_done=set_var("recordingId", "body.firstRecordingId")
                                     + " pm.test('Quellsprache gesetzt', () => pm.expect(body.sourceLanguage).to.eql(pm.variables.get('studioSourceLanguage')));")])),
        R("Status (summary) pollen bis COMPLETED", "GET", status_url, query=[q("summary", "true")],
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           "pm.test('Status nicht FAILED', () => pm.expect(body.status).to.not.eql('FAILED'));",
                           poll_test("['COMPLETED','FAILED'].includes(body.status)", "Transkription")])),
        R("Status je Sprache", "GET", status_url, tests=t_status(200)),
        R("summary + language kombiniert → 400", "GET", status_url,
          query=[q("summary", "true"), q("language", "{{studioSourceLanguage}}")], tests=t_status(400)),
        R("Transkription als SRT", "GET", f"{ST}/recordings/{{{{recordingId}}}}",
          query=[q("task", "transcription"), q("format", "srt")], tests=t_status(200)),
        R("Transkription als VTT", "GET", f"{ST}/recordings/{{{{recordingId}}}}",
          query=[q("task", "transcription"), q("format", "vtt")], tests=t_status(200)),
        R("Untertitel-Segmente (Quelle) lesen", "GET", f"{ST}/projects/{{{{projectId}}}}/recordings/{{{{recordingId}}}}/subtitles",
          query=[q("language", "{{studioSourceLanguage}}"), q("source", "true"), q("dubbing", "false")],
          tests=t_status(200) + "\npm.collectionVariables.set('subtitlesSnapshot', pm.response.text());\nconsole.log(pm.response.text().slice(0, 1500));"),
        R("Untertitel zurückschreiben (unverändert)", "PUT", f"{ST}/projects/{{{{projectId}}}}/recordings/{{{{recordingId}}}}/subtitles",
          pre="""
// Schreibt die eben gelesenen Segmente unverändert zurück (Round-Trip-Test).
let segs = [];
try { const s = JSON.parse(pm.collectionVariables.get('subtitlesSnapshot') || '[]'); segs = Array.isArray(s) ? s : (s.data || s.subtitles || []); } catch (e) {}
pm.collectionVariables.set('subtitlesPayload', JSON.stringify({ language: pm.variables.get('studioSourceLanguage'), source: true, dubbing: false, enqueueTtsUpdate: false, subtitles: segs }));
""",
          body={"mode": "raw", "raw": "{{subtitlesPayload}}", "options": {"raw": {"language": "json"}}},
          tests=t_status(200, 201, 204) + "\npm.collectionVariables.unset('subtitlesSnapshot'); pm.collectionVariables.unset('subtitlesPayload');"),
        R("Übersetzung hinzufügen", "POST", f"{ST}/projects/{{{{projectId}}}}/recordings/{{{{recordingId}}}}/translate",
          body=json_body({"languages": ["{{studioTargetLanguage}}"]}), tests=t_status(200, 201, 202)),
        R("Übersetzung pollen", "GET", status_url, query=[q("language", "{{studioTargetLanguage}}")],
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           poll_test("JSON.stringify(body).toLowerCase().includes('translation')", "Übersetzung")])),
        R("Übersetzung als SRT", "GET", f"{ST}/recordings/{{{{recordingId}}}}",
          query=[q("task", "translation"), q("format", "srt"), q("language", "{{studioTargetLanguage}}")], tests=t_status(200)),
        R("Dubbing hinzufügen", "POST", f"{ST}/projects/{{{{projectId}}}}/recordings/{{{{recordingId}}}}/dubbing",
          body=json_body({"languages": ["{{studioTargetLanguage}}"]}), tests=t_status(200, 201, 202)),
        R("Dubbing pollen", "GET", status_url, query=[q("language", "{{studioTargetLanguage}}")],
          pre=POLL_PRE,
          tests="\n".join([t_status(200), "const body = pm.response.json();",
                           poll_test("JSON.stringify(body).toLowerCase().includes('dubbing')", "Dubbing")])),
        R("Dubbing-Audio (mp3-URLs)", "GET", f"{ST}/recordings/{{{{recordingId}}}}",
          query=[q("task", "dubbing"), q("format", "mp3"), q("language", "{{studioTargetLanguage}}")], tests=t_status(200)),
        R("Projekteinstellungen ändern (PATCH)", "PATCH", f"{ST}/projects/{{{{projectId}}}}",
          body=json_body({"translationModelType": "GPT", "translationModelName": "gpt-5-mini", "ttsProvider": "eleven_v3"}),
          tests=t_status(200)),
        R("Unbekanntes Feld im PATCH → 400", "PATCH", f"{ST}/projects/{{{{projectId}}}}",
          body=json_body({"notAField": True}), tests=t_status(400)),
        R("Projekt in Liste (Suche) finden", "GET", f"{ST}/projects", query=[q("search", "Postman Pipeline")],
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('Projekt gefunden', () => pm.expect(body.data.map(p => p.id)).to.include(pm.collectionVariables.get('projectId')));"),
        R("Cleanup: Projekt löschen", "DELETE", f"{ST}/projects/{{{{projectId}}}}", pre=skip_if_missing("projectId"),
          tests=t_status(200, 204)),
        R("Gelöschtes Projekt → 404", "GET", f"{ST}/projects/{{{{projectId}}}}",
          tests=t_status(404) + "\npm.collectionVariables.unset('projectId'); pm.collectionVariables.unset('recordingId');"),
    ]
    return folder("09 · Studio – Projekt-Pipeline (Transkription → Übersetzung → Dubbing)", items, auth=STUDIO_AUTH, desc=(
        "Legt ein Projekt aus `studioSampleMediaUrl` an, wartet auf die Transkription, liest SRT/VTT und "
        "Untertitel-Segmente (inkl. Round-Trip), fügt Übersetzung und Dubbing hinzu, ändert Einstellungen "
        "und löscht das Projekt.\n\n"
        "⚠️ Verbraucht Studio-Kontingent. Polling-Dauer über `pollIntervalMs`/`pollMaxAttempts` steuern."))


def connector_items(tag, prefix, var, list_body, download_body, *, xliff=True, upload_body=None):
    """Raw/XLIFF round trip for one connector."""
    base = f"{CO}/{prefix}/v1"
    pre = skip_if_missing(var)
    items = [
        R(f"{tag}: Inhalte auflisten", "POST", f"{base}/sync/list-files", headers=[h("X-ActionId", "{{$guid}}")],
          pre=pre, body=json_body(list_body),
          tests="\n".join([t_status(200), t_json_has("remoteResources"),
                           "console.log(JSON.stringify(body.remoteResources.slice(0, 5), null, 2));",
                           "pm.test('X-ActionId zurückgegeben', () => pm.expect(pm.response.headers.has('X-ActionId')).to.be.true);"])),
        R(f"{tag}: Raw herunterladen (storageId)", "POST", f"{base}/sync/download-raw-file",
          headers=[h("X-ResponseType", "ID")], pre=pre, body=json_body(download_body),
          tests=t_status(200) + "\n" + t_json_has("storageId") + "\n" + set_var("rawStorageId", "body.storageId")),
        R(f"{tag}: Raw herunterladen (Binary-Stream)", "POST", f"{base}/sync/download-raw-file",
          headers=[h("X-ResponseType", "OBJECT")], pre=pre, body=json_body(download_body),
          tests=t_status(200) + "\npm.test('Inhalt nicht leer', () => pm.expect(pm.response.responseSize).to.be.above(0));"),
    ]
    if xliff:
        dl_x = dict(download_body)
        items += [
            R(f"{tag}: Als XLIFF herunterladen", "POST", f"{base}/sync/download-xliff-file",
              headers=[h("X-ResponseType", "ID")], pre=pre, body=json_body(dl_x),
              tests=t_status(200) + "\n" + t_json_has("storageId") + "\n" + set_var("xliffStorageId", "body.storageId")),
            R(f"{tag}: Raw → XLIFF konvertieren", "POST", f"{base}/sync/convert-to-xliff",
              headers=[h("X-ResponseType", "ID")], pre=skip_if_missing(var, "rawStorageId"),
              body=json_body({"rawFiles": [{"storageId": "{{rawStorageId}}", "info": {"name": "postman-raw.json"}}],
                              "sourceLocale": "{{connectorSourceLocale}}"}),
              tests=t_status(200) + "\nconst body = pm.response.json();\nif (body.storageId) { " + set_var("convertedXliffStorageId", "body.storageId") + " }"),
            R(f"{tag}: XLIFF → Raw konvertieren", "POST", f"{base}/sync/convert-to-raw",
              headers=[h("X-ResponseType", "ID")], pre=skip_if_missing(var, "xliffStorageId"),
              body=json_body({"storageId": "{{xliffStorageId}}", "sourceLocale": "{{connectorSourceLocale}}",
                              "targetLocale": "{{connectorTargetLocale}}"}),
              tests=t_status(200)),
        ]
        if upload_body is not None:
            items.append(R(f"{tag}: Übersetztes XLIFF hochladen", "POST", f"{base}/sync/upload-xliff-file",
                           pre=skip_if_missing(var, "xliffStorageId") + "\nif (pm.variables.get('connectorAllowWrite') !== 'true') { console.warn('connectorAllowWrite != true → Schreibzugriff übersprungen'); pm.execution.skipRequest && pm.execution.skipRequest(); }",
                           body=json_body({**upload_body, "storageId": "{{xliffStorageId}}"}),
                           tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('success=true', () => pm.expect(body.success, JSON.stringify(body.errors)).to.be.true);"))
    items.append(R(f"{tag}: Async-Listing (Webhook)", "POST", f"{base}/async/list-files",
                   headers=[h("X-Webhook", "{{webhookUrl}}"), h("X-ActionId", "{{$guid}}")],
                   pre=skip_if_missing(var, "webhookUrl"), body=json_body(list_body),
                   tests=t_status(200) + "\n" + t_json_has("requestId", "webHookUrl") + "\npm.test('webHookUrl gespiegelt', () => pm.expect(body.webHookUrl).to.eql(pm.variables.get('webhookUrl')));"))
    items.append(R(f"{tag}: Async ohne X-Webhook → 400", "POST", f"{base}/async/list-files",
                   pre=pre, body=json_body(list_body), tests=t_status(400)))
    return items


def scenario_connectors():
    files = [
        R("Verfügbare Connectoren auflisten", "GET", f"{CO}/connectors/v1",
          tests="\n".join([
              t_status(200), t_json_has("connectors"),
              "const map = { CONTENTFUL2: 'contentfulConnectorUuid', GOOGLE_DRIVE2: 'googleDriveConnectorUuid', GITHUB2: 'githubConnectorUuid',",
              "              BRAZE_MULTILANG: 'brazeConnectorUuid', OPTIMIZELY: 'optimizelyConnectorUuid', TRIDION: 'tridionConnectorUuid' };",
              "// Erste UUID je Typ übernehmen, sofern nicht bereits manuell gesetzt",
              "body.connectors.forEach(c => { const v = map[c.type]; if (v && !pm.collectionVariables.get(v)) { pm.collectionVariables.set(v, c.connectorUuid); console.log(v + ' = ' + c.connectorUuid + ' (' + c.name + ')'); } });",
          ])),
        R("Datei in File Storage hochladen (sync)", "POST", f"{CO}/files/v1/sync/upload-file",
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/sample-upload.txt"}]},
          tests="\n".join([t_status(200), t_json_has("uid", "sha256", "size"),
                           set_var("fileUid", "body.uid"), set_var("storageId", "body.uid"),
                           "pm.collectionVariables.set('uploadedSha256', body.sha256);"])),
        R("Datei wieder herunterladen", "GET", f"{CO}/files/v1/download-file/{{{{fileUid}}}}",
          tests=t_status(200) + "\npm.test('Inhalt identisch', () => pm.expect(pm.response.text()).to.include('Phrase Postman sample upload'));\n"
                "pm.test('SHA-256 identisch', () => pm.expect(CryptoJS.SHA256(pm.response.text()).toString()).to.eql(pm.collectionVariables.get('uploadedSha256')));"),
        R("Datei hochladen (async, Webhook)", "POST", f"{CO}/files/v1/async/upload-file",
          headers=[h("X-Webhook", "{{webhookUrl}}")], pre=skip_if_missing("webhookUrl"),
          body={"mode": "formdata", "formdata": [{"key": "file", "type": "file", "src": "sample-files/sample-upload.txt"}]},
          tests=t_status(200) + "\n" + t_json_has("requestId")),
        R("Unbekannte Datei → 404", "GET", f"{CO}/files/v1/download-file/does-not-exist-{{{{$timestamp}}}}", tests=t_status(400, 404)),
        R("Ohne Token → 401", "GET", f"{CO}/connectors/v1", auth=NOAUTH, tests=t_status(401)),
    ]

    contentful = connector_items(
        "Contentful", "contentful2", "contentfulConnectorUuid",
        {"connectorUuid": "{{contentfulConnectorUuid}}", "configuration": {"tags": ["{{contentfulTag}}"]},
         "path": {"pathType": "ROOT"}, "sourceLocale": "{{connectorSourceLocale}}"},
        {"connectorUuid": "{{contentfulConnectorUuid}}", "configuration": {"tags": ["{{contentfulTag}}"], "includeReferences": True, "includeAssets": False},
         "path": {"pathType": "ENTRY", "spaceId": "{{contentfulSpaceId}}", "environmentId": "{{contentfulEnvironmentId}}", "entryId": "{{contentfulEntryId}}"},
         "sourceLocale": "{{connectorSourceLocale}}"},
        upload_body={"connectorUuid": "{{contentfulConnectorUuid}}", "configuration": {"tags": ["{{contentfulTag}}"], "includeReferences": True},
                     "path": {"pathType": "ROOT"}, "sourceLocale": "{{connectorSourceLocale}}", "targetLocale": "{{connectorTargetLocale}}"},
    )
    gdrive_folder = {"pathType": "FOLDER", "folderId": "{{googleDriveFolderId}}", "drive": {"driveType": "MY_DRIVE"}}
    gdrive = connector_items(
        "Google Drive", "google-drive", "googleDriveConnectorUuid",
        {"connectorUuid": "{{googleDriveConnectorUuid}}", "configuration": {}, "path": {"pathType": "ROOT"}},
        {"connectorUuid": "{{googleDriveConnectorUuid}}", "configuration": {},
         "path": {"pathType": "FILE", "fileId": "{{googleDriveFileId}}", "name": "{{googleDriveFileName}}", "drive": {"driveType": "MY_DRIVE"},
                  "parentChain": [{"id": "root", "name": "My Drive"}]}},
        xliff=False,
    )
    write_guard = "\nif (pm.variables.get('connectorAllowWrite') !== 'true') { console.warn('connectorAllowWrite != true → Schreibzugriff übersprungen'); pm.execution.skipRequest && pm.execution.skipRequest(); }"
    gdrive += [
        R("Google Drive: Upload aus File Storage (STORAGE)", "POST", f"{CO}/google-drive/v1/sync/upload-raw-file",
          headers=[h("X-Upload-Mode", "STORAGE")], pre=skip_if_missing("googleDriveConnectorUuid", "googleDriveFolderId", "storageId") + write_guard,
          body=json_body({"connectorUuid": "{{googleDriveConnectorUuid}}", "configuration": {}, "path": gdrive_folder,
                          "storageId": "{{storageId}}", "name": "postman-upload-{{$timestamp}}.txt"}),
          tests=t_status(200) + "\nconst body = pm.response.json();\npm.test('success=true', () => pm.expect(body.success, JSON.stringify(body.errors)).to.be.true);"),
        R("Google Drive: Streaming-Upload Phase 1 (STREAM init)", "POST", f"{CO}/google-drive/v1/sync/upload-raw-file",
          headers=[h("X-Upload-Mode", "STREAM")], pre=skip_if_missing("googleDriveConnectorUuid", "googleDriveFolderId") + write_guard,
          body=json_body({"connectorUuid": "{{googleDriveConnectorUuid}}", "configuration": {}, "path": gdrive_folder,
                          "name": "postman-stream-{{$timestamp}}.txt", "size": 47}),
          tests="\n".join([t_status(200), t_json_has("uploadUrl", "streamToken", "expiresAt", "maxBytes"),
                           "pm.collectionVariables.set('streamId', body.uploadUrl.split('/').pop());",
                           set_var("streamToken", "body.streamToken")])),
        R("Google Drive: Streaming-Upload Phase 2 (PUT Bytes)", "PUT", f"{CO}/google-drive/v1/sync/upload-raw-file/stream/{{{{streamId}}}}",
          headers=[h("X-Stream-Token", "{{streamToken}}"), h("Content-Type", "text/plain")],
          pre=skip_if_missing("streamId", "streamToken"),
          body={"mode": "raw", "raw": "Phrase Postman sample upload – streaming test.\n"},
          tests=t_status(204) + "\npm.collectionVariables.unset('streamToken'); pm.collectionVariables.unset('streamId');"),
        R("Google Drive: Stream-Token wiederverwenden → Fehler", "PUT", f"{CO}/google-drive/v1/sync/upload-raw-file/stream/invalid-stream",
          headers=[h("X-Stream-Token", "invalid"), h("Content-Type", "text/plain")],
          body={"mode": "raw", "raw": "x"}, tests=t_status(400, 401, 403, 404, 410)),
    ]
    github_item = {"pathType": "ITEM", "owner": "{{githubOwner}}", "repo": "{{githubRepo}}", "branch": "{{githubBranch}}", "location": "{{githubFilePath}}"}
    github = [
        R("GitHub: Branch auflisten (Tree API)", "POST", f"{CO}/github2/v1/sync/list-files",
          pre=skip_if_missing("githubConnectorUuid", "githubOwner", "githubRepo"),
          body=json_body({"connectorUuid": "{{githubConnectorUuid}}", "configuration": {"useTree": True},
                          "path": {"pathType": "BRANCH", "owner": "{{githubOwner}}", "repo": "{{githubRepo}}", "branch": "{{githubBranch}}"},
                          "sourceLocale": "{{connectorSourceLocale}}"}),
          tests=t_status(200) + "\n" + t_json_has("remoteResources")),
        R("GitHub: Datei herunterladen", "POST", f"{CO}/github2/v1/sync/download-raw-file",
          headers=[h("X-ResponseType", "ID")], pre=skip_if_missing("githubConnectorUuid", "githubOwner", "githubRepo"),
          body=json_body({"connectorUuid": "{{githubConnectorUuid}}", "configuration": {}, "path": github_item,
                          "sourceLocale": "{{connectorSourceLocale}}"}),
          tests=t_status(200) + "\n" + t_json_has("storageId") + "\n" + set_var("githubStorageId", "body.storageId")),
        R("GitHub: Batch-Upload als Pull Request", "POST", f"{CO}/github2/v1/sync/upload-batch-raw-files",
          pre=skip_if_missing("githubConnectorUuid", "githubStorageId") + write_guard,
          body=json_body({"connectorUuid": "{{githubConnectorUuid}}",
                          "configuration": {"createPr": True, "prBaseBranch": "{{githubBranch}}", "prTitle": "Phrase Postman test {timeUtc}",
                                            "newBranchOrigin": "{{githubBranch}}"},
                          "path": {"pathType": "BRANCH", "owner": "{{githubOwner}}", "repo": "{{githubRepo}}", "branch": "phrase-postman-test"},
                          "files": [{"location": "postman-test/{{connectorTargetLocale}}.json", "storageId": "{{githubStorageId}}"}]}),
          tests=t_status(200) + "\nconsole.log(pm.response.text());"),
    ]
    generic = []
    for tag, prefix, var in [("Braze", "braze_multilang", "brazeConnectorUuid"), ("Optimizely", "optimizely", "optimizelyConnectorUuid"),
                             ("Tridion Docs", "tridion", "tridionConnectorUuid")]:
        generic.append(R(f"{tag}: Inhalte auflisten (Smoke)", "POST", f"{CO}/{prefix}/v1/sync/list-files",
                         pre=skip_if_missing(var),
                         body=json_body({"connectorUuid": "{{" + var + "}}", "configuration": {}, "path": {"pathType": "ROOT"},
                                         "sourceLocale": "{{connectorSourceLocale}}"}),
                         tests=t_status(200) + "\n" + t_json_has("remoteResources")))

    return [
        folder("10 · Connectors – Discovery & File Storage", files, desc=(
            "Listet konfigurierte Connectoren (und übernimmt deren UUIDs automatisch in die Collection-Variablen), "
            "lädt eine Datei in den File Storage, lädt sie wieder herunter und vergleicht den SHA-256-Hash.")),
        folder("11 · Connectors – Contentful Roundtrip (Raw ⇄ XLIFF)", contentful, desc=(
            "Listing → Raw-Download (ID & Stream) → XLIFF-Download → Konvertierung in beide Richtungen → "
            "(optional) XLIFF-Upload → Async-Variante mit Webhook. Benötigt `contentfulSpaceId`, "
            "`contentfulEnvironmentId`, `contentfulEntryId`. Schreibende Requests nur mit `connectorAllowWrite=true`.")),
        folder("12 · Connectors – Google Drive (inkl. Streaming-Upload)", gdrive, desc=(
            "Listing, Download (ID & Stream), Upload aus File Storage und der zweiphasige Streaming-Upload "
            "(`X-Upload-Mode: STREAM` → `PUT …/stream/{streamId}` mit `X-Stream-Token`). "
            "Hinweis: `size` in Phase 1 muss exakt der Bytezahl des PUT-Bodys entsprechen.")),
        folder("13 · Connectors – GitHub (Download & Batch-PR)", github, desc=(
            "Listet einen Branch über die Tree API, lädt eine Datei herunter und lädt sie als Batch-Commit "
            "in einen neuen PR-Branch hoch (nur mit `connectorAllowWrite=true`).")),
        folder("14 · Connectors – Smoke-Tests Braze / Optimizely / Tridion", generic, desc=(
            "Minimaler Listing-Aufruf je Connector. Wird automatisch übersprungen, wenn keine UUID gesetzt ist.")),
    ]


def scenario_auth_smoke():
    items = [
        R("Control Hub ohne Token → 401", "GET", f"{CH}/groups", auth=NOAUTH, tests=t_status(401)),
        R("Style Guide ohne Token → 401", "POST", f"{SG}/v1/styleguides/search", auth=NOAUTH, body=json_body({}), tests=t_status(401)),
        R("Quality Evaluator ohne Token → 401", "GET", f"{QE}/v1/analytics", auth=NOAUTH, tests=t_status(401)),
        R("Connectors ohne Token → 401", "GET", f"{CO}/connectors/v1", auth=NOAUTH, tests=t_status(401)),
        R("Studio ohne API-Key → 401", "GET", f"{ST}/projects", auth=NOAUTH, tests=t_status(401)),
        R("Control Hub mit gültigem Token → 200", "GET", f"{CH}/groups", query=[q("size", 1)], tests=t_status(200)),
        R("Style Guide mit gültigem Token → 200", "POST", f"{SG}/v1/styleguides/search", body=json_body({"pageNumber": 0, "pageSize": 1}), tests=t_status(200, 403)),
        R("Quality Evaluator mit gültigem Token → 200/403", "GET", f"{QE}/v1/analytics", tests=t_status(200, 403)),
        R("Connectors mit gültigem Token → 200", "GET", f"{CO}/connectors/v1", tests=t_status(200)),
        R("Studio mit gültigem API-Key → 200", "GET", f"{ST}/subtitle-profiles", auth=STUDIO_AUTH, tests=t_status(200)),
    ]
    return folder("00 · Smoke-Test – Erreichbarkeit & Authentifizierung", items, desc=(
        "Schneller Check vor allen anderen Szenarien: jede API einmal ohne und einmal mit Credentials. "
        "403 beim Quality Evaluator bedeutet: Token gültig, aber ohne ADMIN/OWNER-Rolle."))


# --------------------------------------------------------------------------------------
# Assemble
# --------------------------------------------------------------------------------------

RUNTIME_VARS = [
    ("groupId", "Control Hub: zuletzt angelegte Content Group"),
    ("groupIdB", "Control Hub: zweite Gruppe (Negativtests)"),
    ("systemGroupId", "ID der SYSTEM-Content-Group (\"All Groups\")"),
    ("referenceId", "Control Hub: Referenz-ID"),
    ("referenceObjectUid", "Objekt-UID für Referenztests (leer = automatisch generiert)"),
    ("contentGroupId", "Content Group für Style Guides / Rules / Evaluation"),
    ("styleGuideId", "Style Guide ID"),
    ("versionId", "Style-Guide-Versions-ID"),
    ("createJobId", "Async-Create-Job"),
    ("updateJobId", "Async-Update-Job"),
    ("ruleId", "Rule ID"),
    ("projectId", "Studio-Projekt"),
    ("recordingId", "Studio-Recording"),
    ("subtitleProfileId", "Studio Subtitle Profile"),
    ("fileUid", "File-Storage-UID"),
    ("storageId", "storageId für Connector-Uploads"),
    ("streamId", "Google-Drive-Streaming: streamId"),
    ("streamToken", "Google-Drive-Streaming: X-Stream-Token"),
    ("contentfulConnectorUuid", "wird von 'Verfügbare Connectoren auflisten' befüllt oder manuell gesetzt"),
    ("googleDriveConnectorUuid", ""),
    ("githubConnectorUuid", ""),
    ("brazeConnectorUuid", ""),
    ("optimizelyConnectorUuid", ""),
    ("tridionConnectorUuid", ""),
]

ENV_VARS = [
    # key, EU, US, secret, description
    ("accessToken", "", "", True, "IDM-JWT für die Phrase-Platform-APIs (Bearer)"),
    ("studioApiKey", "", "", True, "Studio API-Key (Header X-API-Key)"),
    ("controlHubUrl", "https://eu.phrase.com/control-hub", "https://us.phrase.com/control-hub", False, "Spec nennt nur EU – US-URL analog angenommen"),
    ("styleGuideUrl", "https://eu.phrase.com/styleguide", "https://us.phrase.com/styleguide", False, ""),
    ("qualityEvaluatorUrl", "https://eu.phrase.com/quality-evaluator", "https://us.phrase.com/quality-evaluator", False, ""),
    ("connectorsUrl", "https://eu.phrase.com/connectors", "https://us.phrase.com/connectors", False, ""),
    ("studioUrl", "https://api.studio.eu.phrase.com", "https://api.studio.us.phrase.com", False, ""),
    ("webhookUrl", "", "", False, "Empfänger für Async-Ergebnisse, z. B. eine https://webhook.site/… URL"),
    ("referenceObjectType", "PLATFORM_QA_CHECK", "PLATFORM_QA_CHECK", False, "Objekttyp für Control-Hub-Referenztests"),
    ("tmsProjectUid", "", "", False, "Optional: echtes TMS-Projekt für Style-Guide-Association-Lookups"),
    ("styleGuideLanguage", "de-DE", "de-DE", False, "Sprache des hochgeladenen Style Guides"),
    ("ruleLanguage", "de-DE", "de-DE", False, "Sprache der Test-Rules"),
    ("sourceLocale", "en_us", "en_us", False, "Quality Evaluator Quell-Locale"),
    ("targetLocale", "de_de", "de_de", False, "Quality Evaluator Ziel-Locale"),
    ("studioSourceLanguage", "en", "en", False, "Gesprochene Sprache der Testdatei"),
    ("studioTargetLanguage", "de", "de", False, "Ziel für Übersetzung/Dubbing"),
    ("studioSampleMediaUrl", "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0010_8k.wav",
     "https://www.voiptroubleshooter.com/open_speech/american/OSR_us_000_0010_8k.wav", False, "Kurze öffentliche Sprachaufnahme (~30 s)"),
    ("connectorSourceLocale", "en-US", "en-US", False, ""),
    ("connectorTargetLocale", "de-DE", "de-DE", False, ""),
    ("connectorAllowWrite", "false", "false", False, "true = schreibende Connector-Tests (Upload/PR) ausführen"),
    ("contentfulTag", "translate-me", "translate-me", False, ""),
    ("contentfulSpaceId", "", "", False, ""),
    ("contentfulEnvironmentId", "master", "master", False, ""),
    ("contentfulEntryId", "", "", False, ""),
    ("googleDriveFolderId", "root", "root", False, ""),
    ("googleDriveFileId", "", "", False, ""),
    ("googleDriveFileName", "", "", False, ""),
    ("githubOwner", "", "", False, ""),
    ("githubRepo", "", "", False, ""),
    ("githubBranch", "main", "main", False, ""),
    ("githubFilePath", "locales/en.json", "locales/en.json", False, ""),
    ("pollIntervalMs", "5000", "5000", False, "Wartezeit zwischen Polling-Versuchen"),
    ("pollMaxAttempts", "30", "30", False, "Max. Polling-Versuche je Request"),
    ("maxResponseTimeMs", "30000", "30000", False, "Grenze für den globalen Antwortzeit-Test"),
]


COLLECTION_DESC = """
# Phrase Platform APIs

Vollständige Request-Sammlung für **Control Hub**, **Style Guides & Rules**, **Quality Evaluator**,
**Studio** und **Connectors (Bifrost)** – generiert aus den OpenAPI-Spezifikationen.

## Aufbau
* **📚 API-Referenz** – jeder Endpunkt aus jeder Spec, gruppiert nach API → Tag (→ Sync/Async).
  Optionale Parameter sind deaktiviert vorbelegt, Beispiel-Responses aus der Spec hängen an.
* **🧪 Test-Szenarien** – ausführbare Test-Projekte (Collection Runner / Newman), die Requests über
  Variablen verketten, asynchrone Jobs pollen und am Ende aufräumen.

## Schnellstart
1. Environment **Phrase EU** oder **Phrase US** importieren und auswählen.
2. `accessToken` (IDM-JWT) und `studioApiKey` setzen.
3. *Settings → General → Working Directory* auf den Ordner `postman/phrase` setzen (für Datei-Uploads).
4. Zuerst **00 · Smoke-Test** laufen lassen, dann beliebige Szenarien im Runner.

## Auth
Bearer-Token auf Collection-Ebene; der Ordner **Studio** und die Studio-Szenarien verwenden `X-API-Key`.
""".strip()


def build():
    reference = [build_reference(a) for a in API_DEFS]
    scenarios = [
        scenario_auth_smoke(),
        scenario_control_hub(),
        scenario_control_hub_negative(),
        scenario_style_guide(),
        scenario_rules(),
        scenario_rules_negative(),
        scenario_quality_evaluator(),
        scenario_governance_e2e(),
        scenario_studio_resources(),
        scenario_studio_pipeline(),
        *scenario_connectors(),
    ]
    collection = {
        "info": {
            "_postman_id": uid("collection"),
            "name": "Phrase Platform APIs",
            "description": COLLECTION_DESC,
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "auth": BEARER,
        "event": [event("prerequest", COLLECTION_PRE), event("test", COLLECTION_TEST)],
        "variable": [{"key": k, "value": "", "type": "string", "description": d} for k, d in RUNTIME_VARS],
        "item": [
            folder("📚 API-Referenz", reference, desc="Alle Endpunkte aller Specs. Pfadparameter sind an Collection-Variablen gebunden."),
            folder("🧪 Test-Szenarien", scenarios, desc=(
                "Ausführbare Test-Projekte. Jeder Ordner ist in sich geschlossen (legt seine Testdaten an "
                "und räumt sie wieder auf) und kann einzeln im Collection Runner oder per Newman "
                "(`--folder`) gestartet werden. Polling nutzt `setNextRequest` und funktioniert nur im Runner/Newman.")),
        ],
    }
    out = HERE / "Phrase-Platform-APIs.postman_collection.json"
    out.write_text(json.dumps(collection, indent=2, ensure_ascii=False) + "\n")

    for region, idx in (("EU", 1), ("US", 2)):
        env = {
            "id": uid("env-" + region),
            "name": f"Phrase {region}",
            "values": [
                {"key": k, "value": row[idx], "type": "secret" if row[3] else "default", "enabled": True}
                for row in ENV_VARS for k in [row[0]]
            ],
            "_postman_variable_scope": "environment",
        }
        (HERE / f"Phrase-{region}.postman_environment.json").write_text(json.dumps(env, indent=2, ensure_ascii=False) + "\n")

    return collection


def count(items):
    n = 0
    for it in items:
        n += count(it["item"]) if "item" in it else 1
    return n


if __name__ == "__main__":
    c = build()
    ref, scen = c["item"]
    print(f"API-Referenz: {count(ref['item'])} Requests")
    for f in ref["item"]:
        print(f"  {f['name']}: {count(f['item'])}")
    print(f"Test-Szenarien: {count(scen['item'])} Requests in {len(scen['item'])} Projekten")
    for f in scen["item"]:
        print(f"  {f['name']}: {count(f['item'])}")
