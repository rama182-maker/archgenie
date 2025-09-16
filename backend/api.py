import os
import re
import json
import time
import random
import requests
from typing import List, Dict, Any, Tuple, Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

# ============== Config ==============
load_dotenv()

CAL_API_KEY = os.getenv("CAL_API_KEY", "super-secret-key")

AZURE_OPENAI_ENDPOINT    = (os.getenv("AZURE_OPENAI_ENDPOINT", "") or "").rstrip("/")
AZURE_OPENAI_API_KEY     = os.getenv("AZURE_OPENAI_API_KEY", "")
AZURE_OPENAI_DEPLOYMENT  = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_FORCE_JSON  = os.getenv("AZURE_OPENAI_FORCE_JSON", "true").lower() == "true"

# Pricing knobs
USE_LIVE_AZURE_PRICES = os.getenv("USE_LIVE_AZURE_PRICES", "true").lower() == "true"
HOURS_PER_MONTH = float(os.getenv("HOURS_PER_MONTH", "730"))
DEFAULT_REGION = os.getenv("DEFAULT_REGION", "eastus")
DEFAULT_APPGW_CAPACITY_UNITS = int(os.getenv("DEFAULT_APPGW_CAPACITY_UNITS", "1"))
DEFAULT_SQL_COMPUTE_ONLY     = os.getenv("DEFAULT_SQL_COMPUTE_ONLY", "true").lower() == "true"
DEFAULT_LB_RULES             = int(os.getenv("DEFAULT_LB_RULES", "2"))
DEFAULT_LB_DATA_GB           = float(os.getenv("DEFAULT_LB_DATA_GB", "100"))

# "Never 502" mode: if AOAI or parsing fails, return a safe fallback
FAIL_OPEN = os.getenv("FAIL_OPEN", "true").lower() == "true"

# ============== FastAPI ==============
def require_api_key(x_api_key: str = Header(None)):
    if not x_api_key or x_api_key != CAL_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")

app = FastAPI(title="ArchGenie Azure-Only Backend", version="8.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_credentials=True,
    allow_methods=["*"], allow_headers=["*"],
)

@app.get("/")
def health():
    return {"status": "ok", "message": "Azure-only backend alive"}

# ============== HTTP helpers ==============
def _sleep_backoff(attempt: int, base: float = 0.5, cap: float = 8.0):
    time.sleep(min(cap, base * (2 ** attempt)) * (0.5 + random.random() / 2.0))

def http_post_json(url: str, headers: Dict[str, str], body: dict, max_retries: int = 3, timeout: int = 60):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.post(url, headers=headers, data=json.dumps(body), timeout=timeout)
            if resp.status_code < 300:
                return resp
            if resp.status_code in (408, 409, 429, 500, 502, 503, 504):
                last_err = resp
                if attempt < max_retries:
                    _sleep_backoff(attempt)
                    continue
            return resp
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                _sleep_backoff(attempt)
                continue
            raise
    if isinstance(last_err, requests.Response):
        return last_err
    raise HTTPException(status_code=502, detail=f"POST failed: {last_err}")

def http_get_json(url: str, params: Optional[dict] = None, max_retries: int = 3, timeout: int = 30):
    last_err = None
    for attempt in range(max_retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code < 300:
                return resp
            if resp.status_code in (408, 409, 429, 500, 502, 503, 504):
                last_err = resp
                if attempt < max_retries:
                    _sleep_backoff(attempt)
                    continue
            return resp
        except Exception as e:
            last_err = e
            if attempt < max_retries:
                _sleep_backoff(attempt)
                continue
            raise
    if isinstance(last_err, requests.Response):
        return last_err
    raise HTTPException(status_code=502, detail=f"GET failed: {last_err}")

# ============== AOAI ==============
def _aoai_configured() -> bool:
    return bool(AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY and AZURE_OPENAI_DEPLOYMENT)

def aoai_chat(messages: List[Dict[str, Any]], temperature: float = 0.2) -> Dict[str, Any]:
    if not _aoai_configured():
        raise HTTPException(status_code=500, detail="Azure OpenAI not configured")
    url = (
        f"{AZURE_OPENAI_ENDPOINT}/openai/deployments/"
        f"{AZURE_OPENAI_DEPLOYMENT}/chat/completions?api-version={AZURE_OPENAI_API_VERSION}"
    )
    headers = {"Content-Type": "application/json", "api-key": AZURE_OPENAI_API_KEY}
    body = {"messages": messages, "temperature": temperature}
    if AZURE_OPENAI_FORCE_JSON:
        body["response_format"] = {"type": "json_object"}
    resp = http_post_json(url, headers=headers, body=body, max_retries=3, timeout=60)
    if resp.status_code >= 300:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()

# ============== Text helpers ==============
def strip_fences(text: str) -> str:
    if not text: return ""
    s = text.strip()
    for lang in ("mermaid", "hcl", "terraform", "json"):
        m = re.match(rf"^```{lang}\s*\n([\s\S]*?)```$", s, flags=re.IGNORECASE)
        if m: return m.group(1).strip()
    m = re.match(r"^```\s*\n?([\s\S]*?)```$", s)
    if m: return m.group(1).strip()
    return s

def extract_json_or_fences(content: str) -> Dict[str, Any]:
    if not content:
        return {"diagram": "", "terraform": ""}
    try:
        obj = json.loads(content)
        return {
            "diagram": strip_fences(obj.get("diagram", "")),
            "terraform": strip_fences(obj.get("terraform", "")),
        }
    except Exception:
        pass
    out = {"diagram": "", "terraform": ""}
    m = re.search(r"```mermaid\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["diagram"] = m.group(1).strip()
    m = re.search(r"```(terraform|hcl)\s*\n([\s\S]*?)```", content, flags=re.IGNORECASE)
    if m: out["terraform"] = m.group(2).strip()
    return out

# ============== Mermaid sanitize ==============
def sanitize_mermaid(src: str) -> str:
    if not src:
        return src
    s = src.strip()
    # Ensure header exists; normalize to 'flowchart TD'
    header_re = re.compile(r'^(graph|flowchart)\s+(TD|LR)\b', flags=re.IGNORECASE | re.MULTILINE)
    if header_re.search(s):
        s = header_re.sub("flowchart TD", s, count=1)
    else:
        s = "flowchart TD\n" + s
    # Fix subgraph titles like: subgraph "Title (region)"
    s = re.sub(r'^\s*subgraph\s+([^\[\n"]+)\s*\(([^)]+)\)\s*;?\s*$',
               r'subgraph "\1 (\2)"', s, flags=re.MULTILINE)
    s = re.sub(r'^(?P<hdr>\s*subgraph\b[^\n;]*?);+\s*$', r'\g<hdr>', s, flags=re.MULTILINE)
    # '-. label .->' -> '-. |label| .->'
    s = re.sub(r'-\.\s+([^.|><\-\n][^.|><\-\n]*?)\s+\.\->', r'-. |\1| .->', s)
    # Ensure edge lines end with ';', node lines do not
    out_lines: List[str] = []
    for line in s.splitlines():
        just = line.rstrip().strip()
        if not just:
            out_lines.append("")
            continue
        if just.startswith("subgraph") or just == "end":
            out_lines.append(just)
            continue
        is_edge = ("--" in just) or (".->" in just) or ("---" in just) or ("<->" in just)
        if is_edge:
            if not just.endswith(";"):
                just += ";"
            out_lines.append(just)
        else:
            out_lines.append(just.rstrip(";"))
    s = "\n".join(out_lines)
    # Insert newline after ']' or ')' if followed by token (fixes ']SP')
    s = re.sub(r'(\]|\))\s*(?=[A-Za-z0-9_]+\s*(?:-|\.))', r'\1\n', s)
    # Remove commas from labels
    s = re.sub(r'\[(.*?)\]', lambda m: f"[{m.group(1).replace(',', '')}]", s)
    if not s.endswith("\n"):
        s += "\n"
    return s

# ============== Pricing (Azure only) ==============
_price_cache: Dict[str, Tuple[Any, float]] = {}

def cache_get(key: str):
    v = _price_cache.get(key)
    if not v: return None
    val, exp = v
    return val if exp > time.time() else None

def cache_put(key: str, value, ttl_sec: int = 3600):
    _price_cache[key] = (value, time.time() + ttl_sec)

_REGION_NAME_MAP = {
    "eastus": "US East",
    "eastus2": "US East 2",
    "centralus": "US Central",
    "westus": "US West",
    "westus2": "US West 2",
    "southcentralus": "US South Central",
    "northcentralus": "US North Central",
    "westeurope": "EU West",
    "northeurope": "EU North",
}
def region_variants(region: str) -> List[str]:
    if not region: return []
    r = region.strip()
    variants = set([r, r.lower()])
    mapped = _REGION_NAME_MAP.get(r.lower())
    if mapped:
        variants.add(mapped)
    r_sp = r.replace("-", " ")
    variants.add(r_sp)
    variants.add(r_sp.title())
    if r.lower().endswith("us") and len(r) > 2:
        variants.add(r[:-2].title() + " US")
    return list(variants)

def azure_retail_prices_fetch(filter_str: str, limit: int = 100) -> list:
    base = "https://prices.azure.com/api/retail/prices"
    params = {"api-version": "2023-01-01-preview", "$filter": filter_str}
    out = []
    url = base
    tries = 0
    while True:
        tries += 1
        r = http_get_json(url, params=params if url == base else None, max_retries=2, timeout=30)
        if r.status_code >= 300:
            return []
        j = r.json()
        items = j.get("Items") or []
        out.extend(items)
        if len(out) >= limit:
            return out[:limit]
        next_link = j.get("NextPageLink")
        if not next_link or tries > 20:
            break
        url = next_link
        params = None
    return out

def monthly_from_retail(item: Dict[str, Any]) -> float:
    price = float(item.get("retailPrice") or 0.0)
    uom = (item.get("unitOfMeasure") or "").lower()
    if "hour" in uom:
        return round(price * HOURS_PER_MONTH, 2)
    return round(price, 2)

def azure_price_for_app_service_sku(sku: str, region: str) -> Optional[float]:
    key = f"az.appservice.{region}.{sku}"
    c = cache_get(key)
    if c is not None:
        return c
    service_candidates = ["App Service","App Service Linux","Azure App Service","App Service Plans","Azure App Service Plans"]
    best_price = None
    for reg in region_variants(region):
        for svc in service_candidates:
            flt = f"serviceName eq '{svc}' and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt, limit=60) or []
            if not items:
                alt = f"contains(productName, 'App Service') and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
                items = azure_retail_prices_fetch(alt, limit=60) or []
            hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
            pool = hourly or items
            for it in pool:
                m = monthly_from_retail(it)
                if best_price is None or (m and m < best_price):
                    best_price = m
    if best_price is not None:
        cache_put(key, best_price)
    return best_price

def azure_price_for_vm_size(size: str, region: str) -> Optional[float]:
    key = f"az.vm.{region}.{size}"
    c = cache_get(key)
    if c is not None:
        return c
    best_price = None
    for reg in region_variants(region):
        candidates = [size, size.replace("_", " "), size.replace("v", " v")]
        for sku in candidates:
            flt = f"serviceName eq 'Virtual Machines' and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt, limit=80) or []
            hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
            pool = hourly or items
            for it in pool:
                m = monthly_from_retail(it)
                if best_price is None or (m and m < best_price):
                    best_price = m
    if best_price is not None:
        cache_put(key, best_price)
    return best_price

def azure_price_for_sql(sku: str, region: str) -> Optional[float]:
    key = f"az.sql.{region}.{sku}"
    c = cache_get(key)
    if c is not None:
        return c
    best_price = None
    bad_words = ["backup", "storage", "io", "data processed", "per gb", "gb-month"]
    good_words = ["dtu", "vcore", "compute"]
    def is_compute_meter(it):
        meter = (it.get("meterName") or "").lower()
        if any(b in meter for b in bad_words):
            return False
        return any(g in meter for g in good_words) or sku.lower() in meter
    for reg in region_variants(region):
        flt1 = f"serviceName eq 'SQL Database' and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
        items = azure_retail_prices_fetch(flt1, limit=200) or []
        if not items:
            flt2 = f"contains(productName, 'SQL Database') and skuName eq '{sku}' and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt2, limit=200) or []
        if not items:
            flt3 = f"serviceName eq 'SQL Database' and contains(meterName, '{sku}') and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(flt3, limit=200) or []
        hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
        pool = hourly or items
        pool = [x for x in pool if is_compute_meter(x)] if DEFAULT_SQL_COMPUTE_ONLY else pool
        for it in pool:
            m = monthly_from_retail(it)
            if best_price is None or (m and m < best_price):
                best_price = m
    if best_price is not None:
        cache_put(key, best_price)
    return best_price

def azure_price_for_storage_lrs_per_gb(region: str) -> Optional[float]:
    key = f"az.storage.lrs.{region}"
    c = cache_get(key)
    if c is not None:
        return c
    best = None
    for reg in region_variants(region):
        flt = f"serviceName eq 'Storage' and armRegionName eq '{reg}' and contains(skuName, 'LRS') and retailPrice ne 0"
        items = azure_retail_prices_fetch(flt, limit=80) or []
        for it in items:
            m = monthly_from_retail(it)  # per GB-month
            if best is None or (m and m < best):
                best = m
    if best is not None:
        cache_put(key, best)
    return best

def azure_price_for_lb_components(region: str) -> Optional[Dict[str, float]]:
    key = f"az.lb.standard.components.{region}"
    cached = cache_get(key)
    if cached:
        return cached
    best_rule = None
    best_data = None
    def is_rule_meter(it):
        meter = (it.get("meterName") or "").lower()
        uom = (it.get("unitOfMeasure") or "").lower()
        return "rule" in meter and "hour" in uom
    def is_data_meter(it):
        meter = (it.get("meterName") or "").lower()
        uom = (it.get("unitOfMeasure") or "").lower()
        return ("data" in meter or "processed" in meter or "data path" in meter) and ("gb" in uom)
    for reg in region_variants(region):
        flt = f"serviceName eq 'Load Balancer' and armRegionName eq '{reg}' and retailPrice ne 0"
        items = azure_retail_prices_fetch(flt, limit=200) or []
        hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
        for it in hourly:
            if is_rule_meter(it):
                m = monthly_from_retail(it)
                if best_rule is None or (m and m < best_rule):
                    best_rule = m
        for it in items:
            if is_data_meter(it):
                m = monthly_from_retail(it)
                if best_data is None or (m and m < best_data):
                    best_data = m
        if best_rule is None or best_data is None:
            alt = f"contains(productName, 'Load Balancer') and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(alt, limit=200) or []
            hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
            for it in hourly:
                if is_rule_meter(it):
                    m = monthly_from_retail(it)
                    if best_rule is None or (m and m < best_rule):
                        best_rule = m
            for it in items:
                if is_data_meter(it):
                    m = monthly_from_retail(it)
                    if best_data is None or (m and m < best_data):
                        best_data = m
    if best_rule is None and best_data is None:
        return None
    comps = {"rule_hour_monthly": round(best_rule or 0.0, 4), "data_gb_monthly": round(best_data or 0.0, 4)}
    cache_put(key, comps, ttl_sec=3600)
    return comps

def azure_price_for_appgw_wafv2_components(region: str) -> Optional[Dict[str, float]]:
    key = f"az.appgw.wafv2.components.{region}"
    cached = cache_get(key)
    if cached:
        return cached
    best_base = None
    best_cu = None
    def is_base(it):
        meter = (it.get("meterName") or "").lower()
        uom = (it.get("unitOfMeasure") or "").lower()
        if "gb" in uom:
            return False
        return ("gateway" in meter or "waf v2" in meter or "app gateway" in meter) and "capacity" not in meter
    def is_cu(it):
        meter = (it.get("meterName") or "").lower()
        uom = (it.get("unitOfMeasure") or "").lower()
        return "capacity unit" in meter and "hour" in uom
    for reg in region_variants(region):
        flt = f"serviceName eq 'Application Gateway' and armRegionName eq '{reg}' and retailPrice ne 0"
        items = azure_retail_prices_fetch(flt, limit=200) or []
        hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
        for it in hourly:
            m = monthly_from_retail(it)
            if is_base(it):
                if best_base is None or (m and m < best_base):
                    best_base = m
            elif is_cu(it):
                if best_cu is None or (m and m < best_cu):
                    best_cu = m
        if best_base is None or best_cu is None:
            alt = f"contains(productName, 'Application Gateway') and armRegionName eq '{reg}' and retailPrice ne 0"
            items = azure_retail_prices_fetch(alt, limit=200) or []
            hourly = [x for x in items if "hour" in (x.get("unitOfMeasure","").lower())]
            for it in hourly:
                m = monthly_from_retail(it)
                if is_base(it):
                    if best_base is None or (m and m < best_base):
                        best_base = m
                elif is_cu(it):
                    if best_cu is None or (m and m < best_cu):
                        best_cu = m
    if best_base is None and best_cu is None:
        return None
    components = {"base_monthly": round(best_base or 0.0, 2), "capacity_unit_monthly": round(best_cu or 0.0, 2)}
    cache_put(key, components, ttl_sec=3600)
    return components

def price_items(items: List[dict]) -> dict:
    currency = "USD"
    notes: List[str] = []
    total = 0.0
    out_items = []
    for it in items:
        cloud   = it.get("cloud","").lower()
        service = it.get("service","").lower()
        sku     = it.get("sku","")
        qty     = int(it.get("qty", 1) or 1)
        region  = it.get("region") or DEFAULT_REGION
        size_gb = float(it.get("size_gb", 0) or 0)
        hours   = float(it.get("hours", HOURS_PER_MONTH) or HOURS_PER_MONTH)
        unit_monthly: Optional[float] = None
        if cloud == "azure" and USE_LIVE_AZURE_PRICES:
            try:
                if service == "app_service":
                    unit_monthly = azure_price_for_app_service_sku(sku, region)
                elif service == "vm":
                    unit_monthly = azure_price_for_vm_size(sku, region)
                elif service == "azure_sql":
                    unit_monthly = azure_price_for_sql(sku, region)
                elif service == "storage":
                    per_gb = azure_price_for_storage_lrs_per_gb(region)
                    if per_gb is not None:
                        unit_monthly = per_gb * (size_gb if size_gb > 0 else 100.0)
                elif service == "lb":
                    comps = azure_price_for_lb_components(region)
                    if comps:
                        rules = int(it.get("rules") or DEFAULT_LB_RULES)
                        data_gb = float(it.get("data_gb") or DEFAULT_LB_DATA_GB)
                        unit_monthly = (rules * comps["rule_hour_monthly"]) + (data_gb * comps["data_gb_monthly"])
                        if not it.get("rules"):
                            notes.append(f"LB rules defaulted to {rules}/h.")
                        if not it.get("data_gb"):
                            notes.append(f"LB data processed defaulted to {data_gb} GB/mo.")
                    else:
                        unit_monthly = None
                elif service == "app_gateway":
                    comps = azure_price_for_appgw_wafv2_components(region)
                    if comps:
                        cu = int(it.get("capacity_units") or it.get("size_gb") or DEFAULT_APPGW_CAPACITY_UNITS)
                        unit_monthly = comps["base_monthly"] + cu * comps["capacity_unit_monthly"]
                        if not it.get("capacity_units") and not it.get("size_gb"):
                            notes.append(f"App Gateway capacity units defaulted to {cu}/h.")
                    else:
                        unit_monthly = None
                elif service == "redis":
                    # Optional: add azure_price_for_redis if you want Redis priced
                    unit_monthly = 0.0
                elif service == "monitor":
                    # Optional: add azure_price_for_log_analytics
                    unit_monthly = 0.0
                elif service == "aks":
                    notes.append("AKS control plane free; worker node VM costs not included.")
                    unit_monthly = 0.0
            except Exception as e:
                notes.append(f"Lookup failed for {cloud}:{service}:{sku} in {region}: {e}")
                unit_monthly = None
        if unit_monthly is None:
            notes.append(f"No price found for {cloud}:{service}:{sku} in {region} (set $0).")
            unit_monthly = 0.0
        monthly = float(unit_monthly)
        if hours and hours != HOURS_PER_MONTH and monthly > 0:
            monthly = monthly * (hours / HOURS_PER_MONTH)
        monthly = round(monthly * qty, 2)
        total += monthly
        out_line = {
            "cloud": cloud, "service": service, "sku": sku,
            "qty": qty, "region": region,
            "size_gb": size_gb if size_gb > 0 else None,
            "hours": hours if hours and hours != HOURS_PER_MONTH else None,
            "unit_monthly": round(unit_monthly, 2),
            "monthly": monthly
        }
        if service == "app_gateway":
            out_line["capacity_units"] = int(it.get("capacity_units") or it.get("size_gb") or DEFAULT_APPGW_CAPACITY_UNITS)
        if service == "lb":
            out_line["rules"] = int(it.get("rules") or DEFAULT_LB_RULES)
            out_line["data_gb"] = float(it.get("data_gb") or DEFAULT_LB_DATA_GB)
        out_items.append(out_line)
    return {"currency": currency, "total_estimate": round(total, 2), "items": out_items}

# ============== Normalization from text/diagram/tf ==============
def normalize_to_items(ask: str = "", diagram: str = "", tf: str = "", region: Optional[str] = None) -> List[dict]:
    region = region or DEFAULT_REGION
    items: List[dict] = []
    blob = f"{ask}\n{diagram}\n{tf}".lower()
    def add(cloud, service, sku, qty=1, size_gb=None):
        d = {"cloud": cloud, "service": service, "sku": sku, "qty": max(1, int(qty)), "region": region}
        if size_gb is not None: d["size_gb"] = float(size_gb)
        items.append(d)
    if re.search(r"\bapp service\b|\bweb app\b", blob):
        qty = 2 if re.search(r"\bfront.*back|backend.*front", blob) else 1
        add("azure", "app_service", "S1", qty=qty)
    if re.search(r"\b(mssql|azure sql|sql database)\b", blob):
        add("azure", "azure_sql", "S0", qty=1)
        m = re.search(r"(\d+)\s*gb", blob)
        if m: items[-1]["size_gb"] = float(m.group(1))
    vm_hits = len(re.findall(r"\bvmss\b|\bvm scale set\b|\bvirtual machine\b|\bvm\b", blob))
    if vm_hits: add("azure", "vm", "B2s", qty=vm_hits)
    if re.search(r"\bstorage account\b|\bblob storage\b|\bazurerm_storage", blob):
        add("azure", "storage", "LRS", qty=1, size_gb=100)
    if re.search(r"\bapplication gateway\b|\bapp gateway\b|\bapp gw\b", blob):
        add("azure", "app_gateway", "WAF_v2", qty=1)
        m_cu = re.search(r"(\d+)\s*(?:capacity\s*units|cu)\b", blob)
        if m_cu and items: items[-1]["capacity_units"] = int(m_cu.group(1))
    elif re.search(r"\bload balancer\b|\blb\b", blob):
        add("azure", "lb", "Standard", qty=1)
        m_rules = re.search(r"(\d+)\s*(?:rules|lb\s*rules)", blob)
        m_data  = re.search(r"(\d+)\s*gb\s*(?:data|processed)", blob)
        if m_rules and items: items[-1]["rules"] = int(m_rules.group(1))
        if m_data and items:  items[-1]["data_gb"] = float(m_data.group(1))
    if "aks" in blob or "kubernetes service" in blob:
        add("azure", "aks", "standard", qty=1)
    return items

# ============== Public Endpoint (Azure only) ==============
@app.post("/mcp/azure/diagram-tf")
def azure_mcp(payload: dict = Body(...), _=Depends(require_api_key)):
    app_name = payload.get("app_name", "3-tier web app")
    extra = payload.get("prompt") or ""
    region = payload.get("region") or DEFAULT_REGION
    system = (
        "You are ArchGenie's Azure MCP.\n"
        "Return ONLY a single JSON object with keys:\n"
        '{\n'
        '  "diagram": "Mermaid code starting with: graph TD or flowchart TD",\n'
        '  "terraform": "Valid Terraform HCL for Azure (resource group, app service plan, web apps, sql, etc.)"\n'
        '}\n'
        "Do not write explanations, backticks, or any other keys. JSON only."
    )
    user = (
        f"Create an Azure architecture for: {app_name}.\n"
        f"Extra requirements: {extra}\n"
        f"Region: {region}\n"
        "Output JSON only."
    )
    try:
        result = aoai_chat([{"role": "system", "content": system}, {"role": "user", "content": user}], temperature=0.2)
        content = result["choices"][0]["message"]["content"]
        parsed = extract_json_or_fences(content)
        diagram_raw = (parsed.get("diagram") or "").strip()
        tf_raw      = (parsed.get("terraform") or "").strip()
        if not diagram_raw or not tf_raw:
            raise ValueError("Model missing diagram/terraform")
        diagram = sanitize_mermaid(diagram_raw)
        tf      = strip_fences(tf_raw)
    except Exception as e:
        if not FAIL_OPEN:
            raise
        # Safe fallback so FE always renders
        diagram = sanitize_mermaid("""flowchart TD
  A[Internet] -->|HTTPS| B[Azure Front Door];
  B -->|HTTPS| C[Azure Application Gateway];
  C -->|WAF| H[Web Application Firewall];
  C -->|HTTPS| D[Web Tier - Azure App Service];
  D -->|HTTPS| E[Application Tier - Azure App Service];
  E -->|TCP 1433| F[Azure SQL Database];
""")
        tf = "# Terraform generation failed-open; check backend logs"
    # Best-effort Azure cost
    items = normalize_to_items(ask=extra or app_name, diagram=diagram, tf=tf, region=region)
    cost = price_items(items)
    return {"diagram": diagram, "terraform": tf, "cost": cost}