import os
import socket
import time
import concurrent.futures
import base64
import json
import urllib.parse
import re
import requests
import yaml

try:
    from playwright.sync_api import sync_playwright
except Exception:
    sync_playwright = None

STATS_FILE = "source_stats.json"
MAX_FAIL_COUNT = 3

# 默认走“全量抓取 + 全量推送”模式；只有在显式打开 STRICT_FILTER_MODE=true 时才启用严格筛选
STRICT_FILTER_MODE = os.environ.get("STRICT_FILTER_MODE", "false").lower() == "true"
MAX_PROXY_OUTPUT = int(os.environ.get("MAX_PROXY_OUTPUT", "3000"))
MAX_LATENCY_MS = int(os.environ.get("MAX_LATENCY_MS", "99999"))
MAX_THREAD_WORKERS = int(os.environ.get("MAX_THREAD_WORKERS", "30"))
DEFAULT_PASTE_TO_SOURCES = [
    "https://paste.to/?14372a45d5d8fbb7#771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb",
]
PASTE_TO_SPECIAL_ID = "14372a45d5d8fbb7"
PASTE_TO_SPECIAL_HASH = "771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb"


def load_failure_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def save_failure_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存统计状态异常: {e}")


def append_unique_source(source_url, filename="123.txt"):
    if not source_url:
        return False
    source_url = str(source_url).strip()
    if not source_url:
        return False
    if source_url.startswith("#"):
        return False
    if not source_url.lower().startswith(("http://", "https://")):
        source_url = f"https://{source_url}"
    source_url = source_url.split("#", 1)[0].strip()
    if not source_url:
        return False
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            existing = {line.strip() for line in f if line.strip()}
        if source_url in existing:
            return False
    with open(filename, "a", encoding="utf-8") as f:
        f.write(source_url + "\n")
    return True


def load_sources_from_file(filename="123.txt"):
    if not os.path.exists(filename):
        return []
    sources = []
    try:
        with open(filename, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    sources.append(line)
    except Exception:
        return []
    return list(dict.fromkeys(sources))


def update_123_txt_file(removed_urls):
    if not os.path.exists("123.txt") or not removed_urls:
        return
    try:
        with open("123.txt", "r", encoding="utf-8") as f:
            lines = f.readlines()

        new_lines = []
        for line in lines:
            clean_line = line.strip()
            if clean_line in removed_urls:
                print(f"🗑️ 已自动从 123.txt 中淘汰并清理失效源: {clean_line}")
                continue
            new_lines.append(line)

        with open("123.txt", "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"⚠️ 更新 123.txt 失败: {e}")


def country_code_to_emoji(country_code):
    if not country_code or len(country_code) != 2:
        return "🏳️"
    return "".join(chr(127397 + ord(c)) for c in country_code.upper())


def normalize_proxy_name(name, default):
    text = str(name or default).strip()
    return text if text else default


def ensure_safe_proxy(proxy):
    if not isinstance(proxy, dict):
        return None
    server = str(proxy.get("server", "")).strip()
    if not server:
        return None
    try:
        port = int(proxy.get("port", 0))
    except (TypeError, ValueError):
        return None
    if not (1 <= port <= 65535):
        return None
    proxy["server"] = server
    proxy["port"] = port
    proxy["type"] = str(proxy.get("type", "unknown")).strip() or "unknown"
    proxy["name"] = normalize_proxy_name(proxy.get("name"), f"{proxy['type'].upper()}-{server}:{port}")
    return proxy


def proxy_signature(proxy):
    proxy = ensure_safe_proxy(proxy)
    if not proxy:
        return None
    return (
        str(proxy.get("type", "")).lower(),
        str(proxy.get("server", "")).lower(),
        int(proxy.get("port", 0)),
        str(proxy.get("uuid", "")).strip(),
        str(proxy.get("password", "")).strip(),
        str(proxy.get("cipher", "")).strip(),
    )


def deduplicate_proxies(proxies):
    seen = set()
    result = []
    for proxy in proxies:
        sig = proxy_signature(proxy)
        if sig is None:
            continue
        if sig in seen:
            continue
        seen.add(sig)
        result.append(ensure_safe_proxy(proxy))
    return result


def make_unique_proxy_name(proxy, seen_names):
    base_name = str(proxy.get("name") or f"{proxy.get('type', 'proxy')}-{proxy.get('server')}:{proxy.get('port')}").strip() or "Proxy"
    candidate = base_name
    suffix = 2
    while candidate in seen_names:
        candidate = f"{base_name} ({suffix})"
        suffix += 1
    seen_names.add(candidate)
    proxy["name"] = candidate
    return candidate


def batch_get_locations(proxies):
    queries = []
    ip_to_proxy_indices = {}

    for idx, proxy in enumerate(proxies):
        server = proxy.get("server")
        if not server:
            continue
        try:
            ip = socket.gethostbyname(server)
            queries.append({"query": ip, "fields": "status,country,countryCode"})
            ip_to_proxy_indices.setdefault(ip, []).append(idx)
        except Exception:
            continue

    if not queries:
        for proxy in proxies:
            proxy.setdefault("geo_country", "其他")
            proxy.setdefault("geo_flag", "🌍")
        return proxies

    batch_url = "http://ip-api.com/batch?lang=zh-CN"
    try:
        response = requests.post(batch_url, json=queries, timeout=10)
        if response.status_code == 200:
            for res in response.json():
                if res.get("status") == "success":
                    ip = res.get("query")
                    country = res.get("country", "未知")
                    code = res.get("countryCode", "")
                    flag = country_code_to_emoji(code)
                    for idx in ip_to_proxy_indices.get(ip, []):
                        proxies[idx]["geo_country"] = country
                        proxies[idx]["geo_flag"] = flag
    except Exception as e:
        print(f"⚠️ 批量查询地理位置异常: {e}")

    for proxy in proxies:
        proxy.setdefault("geo_country", "其他")
        proxy.setdefault("geo_flag", "🌍")

    return proxies


def test_tcp_latency(server, port, timeout=3):
    start_time = time.time()
    try:
        with socket.create_connection((server, int(port)), timeout=timeout):
            latency = (time.time() - start_time) * 1000
            return round(latency, 2)
    except Exception:
        return None


def proxy_priority(proxy):
    latency = float(proxy.get("latency", 99999))
    country = str(proxy.get("geo_country", "其他")).strip()
    score = latency
    if country in ("其他", "未知"):
        score += 300
    if proxy.get("type") in {"ssr", "unknown"}:
        score += 120
    return score


def strict_filter_proxies(valid_proxies, max_output=None, max_latency_ms=None):
    if max_output is None:
        max_output = MAX_PROXY_OUTPUT
    if max_latency_ms is None:
        max_latency_ms = MAX_LATENCY_MS

    strict = []
    for proxy in valid_proxies:
        latency = float(proxy.get("latency", 99999))
        country = str(proxy.get("geo_country", "其他")).strip()
        if latency > max_latency_ms:
            continue
        if country in ("其他", "未知"):
            continue
        strict.append(proxy)

    if not strict:
        strict = [p for p in valid_proxies if float(p.get("latency", 99999)) <= max_latency_ms]

    strict.sort(key=lambda p: (proxy_priority(p), p.get("latency", 99999)))

    deduped = []
    seen = set()
    for proxy in strict:
        key = (str(proxy.get("server", "")).lower(), int(proxy.get("port", 0)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(proxy)

    return deduped[:max_output]


def select_final_proxies(valid_proxies):
    # 默认全量推送：只做 server+port 去重，并限制最多输出数量
    if STRICT_FILTER_MODE:
        return strict_filter_proxies(valid_proxies, MAX_PROXY_OUTPUT, MAX_LATENCY_MS)

    deduped = []
    seen = set()
    for proxy in sorted(valid_proxies, key=lambda p: (p.get("latency") is None, p.get("latency") or 999999, p.get("geo_country") == "其他")):
        key = (str(proxy.get("server", "")).lower(), int(proxy.get("port", 0)))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(proxy)
    return deduped[:MAX_PROXY_OUTPUT]


def extract_numeric_keys(text):
    if not text:
        return []
    found = set()
    for match in re.finditer(r"(?<!\d)(\d{4})(?!\d)", text):
        val = match.group(1)
        if 1000 <= int(val) <= 9999:
            found.add(val)
    return sorted(found)


def _decode_numeric_xor(payload, key):
    try:
        blob = payload.encode("utf-8")
        key_bytes = str(key).encode("utf-8")
        out = bytes(b ^ key_bytes[i % len(key_bytes)] for i, b in enumerate(blob))
        return out.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _decode_numeric_add_sub(payload, key, mode="add"):
    try:
        blob = payload.encode("utf-8")
        k = int(key)
        if mode == "add":
            out = bytes((b + k) % 256 for b in blob)
        elif mode == "sub":
            out = bytes((b - k) % 256 for b in blob)
        elif mode == "xor":
            out = bytes(b ^ k for b in blob)
        else:
            return ""
        return out.decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _extract_potential_cipher_payloads(text):
    blobs = set()
    pattern = r"[A-Za-z0-9+/=_-]{20,}"
    for item in re.findall(pattern, text):
        if len(item) >= 20 and not item.lower().startswith(("http", "https")):
            blobs.add(item)
    for item in re.findall(r'"([A-Za-z0-9+/=_-]{20,})"', text):
        blobs.add(item)
    for item in re.findall(r"'([A-Za-z0-9+/=_-]{20,})'", text):
        blobs.add(item)
    return sorted(blobs)


def brute_force_numeric_cipher_links(text):
    if not text:
        return []
    keys = extract_numeric_keys(text)
    if not keys:
        keys = [str(i).zfill(4) for i in range(1000, 10000)]

    payloads = _extract_potential_cipher_payloads(text)
    if not payloads:
        payloads = list(set(re.findall(r"[A-Za-z0-9+/=_-]{16,}", text)))

    found = []
    seen = set()

    for key in keys[:1000]:
        if len(found) >= 20:
            break
        for payload in payloads:
            if len(payload) < 16:
                continue
            for decoder in (
                (lambda p, k: _decode_numeric_xor(p, k)),
                (lambda p, k: _decode_numeric_add_sub(p, k, "add")),
                (lambda p, k: _decode_numeric_add_sub(p, k, "sub")),
                (lambda p, k: _decode_numeric_add_sub(p, k, "xor")),
            ):
                decoded = decoder(payload, key)
                if not decoded:
                    continue
                if any(marker in decoded.lower() for marker in ("http://", "https://", "vmess://", "ss://", "trojan://", "clash", "subscribe", "yaml", "yml")):
                    for match in re.findall(r'https?://[^\s\"\'<>]+', decoded):
                        candidate = match.rstrip(")].,;\n\r")
                        if candidate and candidate not in seen:
                            seen.add(candidate)
                            found.append(candidate)
                    for match in re.findall(r'(?:vmess://|ss://|trojan://)[^\s\"\'<>]+', decoded):
                        candidate = match.rstrip(")].,;\n\r")
                        if candidate and candidate not in seen:
                            seen.add(candidate)
                            found.append(candidate)
                    if len(found) >= 20:
                        return found
    return found


def extract_subscription_links_from_text(content):
    if not content:
        return []

    candidate_urls = []
    for match in re.findall(r'https?://[^\s\"\'<>]+', content):
        candidate = match.rstrip(")].,;\n\r")
        lower = candidate.lower()
        if any(keyword in lower for keyword in ("clash", "yaml", "yml", "subscribe", "sub", "v2ray", "trojan", "vmess", "ss://", "ssr://")):
            candidate_urls.append(candidate)
            continue
        if candidate.startswith("http") and any(token in lower for token in ("raw", "gist", "githubusercontent")):
            candidate_urls.append(candidate)
    cleaned = []
    seen = set()
    for url in candidate_urls:
        href = urllib.parse.unquote(url)
        if href not in seen:
            seen.add(href)
            cleaned.append(href)

    if cleaned:
        return cleaned

    numeric_decoded = brute_force_numeric_cipher_links(content)
    if numeric_decoded:
        return numeric_decoded

    return []


def fetch_page_via_browser(url, timeout_seconds=30):
    if sync_playwright is None:
        return ""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 2000})
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_seconds * 1000)
            page.wait_for_timeout(3000)
            html = page.content()
            text = page.evaluate("() => document.body ? document.body.innerText || '' : ''")
            browser.close()
            return f"{html}\n{text}"
    except Exception:
        return ""


def build_special_paste_candidates(url):
    parsed = urllib.parse.urlparse(url)
    candidates = []
    paste_id = parsed.query or parsed.path.strip("/")
    if paste_id in ("", "/"):
        paste_id = PASTE_TO_SPECIAL_ID
    if "14372a45d5d8fbb7" in url:
        paste_id = PASTE_TO_SPECIAL_ID
    if paste_id:
        candidates.extend([
            f"https://paste.to/{paste_id}",
            f"https://paste.to/raw/{paste_id}",
            f"https://paste.to/?{paste_id}",
            f"https://paste.to/?{paste_id}#{parsed.fragment}",
            f"https://paste.to/raw/{paste_id}?token={parsed.fragment}",
            f"https://paste.to/raw/{paste_id}#{parsed.fragment}",
        ])
    if parsed.fragment:
        candidates.extend([
            f"https://paste.to/raw/{PASTE_TO_SPECIAL_ID}?token={parsed.fragment}",
            f"https://paste.to/raw/{PASTE_TO_SPECIAL_ID}#{parsed.fragment}",
            f"https://paste.to/?{PASTE_TO_SPECIAL_ID}#{parsed.fragment}",
        ])
    candidates.extend([
        "https://paste.to/?14372a45d5d8fbb7",
        "https://paste.to/14372a45d5d8fbb7",
        "https://paste.to/raw/14372a45d5d8fbb7",
        "https://paste.to/?14372a45d5d8fbb7#771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb",
    ])
    unique = []
    seen = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def discover_paste_to_sources(url, session):
    if "paste.to" not in url.lower():
        return []

    discovered = []
    page_content = ""

    try:
        response = session.get(url, timeout=20)
        if response.status_code == 200:
            page_content = response.text or ""
    except Exception:
        page_content = ""

    browser_content = fetch_page_via_browser(url)
    if browser_content:
        page_content = browser_content if not page_content else page_content + "\n" + browser_content

    if page_content:
        discovered.extend(extract_subscription_links_from_text(page_content))

    if "14372a45d5d8fbb7" in url or "771Xe1zSng91Tx3prCuwVyDeT3hBy9gmsiCBaPDUszb" in url:
        for candidate in build_special_paste_candidates(url):
            leads = []
            try:
                r = session.get(candidate, timeout=20)
                if r.status_code == 200:
                    leads.append(r.text or "")
            except Exception:
                pass
            if not leads:
                browser_text = fetch_page_via_browser(candidate)
                if browser_text:
                    leads.append(browser_text)
            for content in leads:
                discovered.extend(extract_subscription_links_from_text(content))
                if not discovered:
                    discovered.extend(brute_force_numeric_cipher_links(content))

    parsed = urllib.parse.urlparse(url)
    raw_candidates = []
    if parsed.query:
        raw_candidates.extend([
            f"https://paste.to/raw/{parsed.query.split('&', 1)[0]}",
            f"https://paste.to/{parsed.query.split('&', 1)[0]}",
        ])
    if parsed.path and parsed.path not in ("/", ""):
        path_id = parsed.path.strip("/")
        if path_id and path_id not in {"?", ""}:
            raw_candidates.extend([
                f"https://paste.to/raw/{path_id}",
                f"https://paste.to/{path_id}",
            ])
    fragment = parsed.fragment
    if fragment:
        raw_candidates.extend([
            f"https://paste.to/raw/{fragment}",
            f"https://paste.to/{fragment}",
        ])

    for candidate in list(dict.fromkeys(raw_candidates)):
        if not candidate or "paste.to" not in candidate.lower():
            continue
        content = ""
        try:
            raw_response = session.get(candidate, timeout=20)
            if raw_response.status_code == 200:
                content = raw_response.text or ""
        except Exception:
            content = ""
        if not content:
            content = fetch_page_via_browser(candidate)
        if content:
            discovered.extend(extract_subscription_links_from_text(content))

    if not discovered:
        discovered.extend(brute_force_numeric_cipher_links(page_content or ""))

    return list(dict.fromkeys(discovered))


def auto_discover_source_urls(session):
    discovered = []
    sources = list(DEFAULT_PASTE_TO_SOURCES)
    env_sources = os.environ.get("AUTO_PASTE_URLS", "")
    if env_sources:
        sources.extend([s.strip() for s in env_sources.split(",") if s.strip()])

    for url in sources:
        for found in discover_paste_to_sources(url, session):
            if found not in discovered:
                discovered.append(found)
    return discovered


# ==================== 万能订阅内容解析器 ====================
def parse_vmess_link(link):
    try:
        content = link[8:]
        padding = 4 - (len(content) % 4)
        if padding < 4:
            content += "=" * padding
        decoded = base64.b64decode(content).decode("utf-8", errors="ignore")
        data = json.loads(decoded)
        return {
            "name": data.get("ps", "Vmess-Node"),
            "type": "vmess",
            "server": data.get("add"),
            "port": int(data.get("port", 443)),
            "uuid": data.get("id"),
            "alterId": int(data.get("aid", 0)),
            "cipher": "auto",
            "network": data.get("net", "tcp"),
            "tls": True if data.get("tls") == "tls" else False,
            "ws-opts": {"path": data.get("path", "/"), "headers": {"Host": data.get("host", "")}} if data.get("net") == "ws" else None,
        }
    except Exception:
        return None


def parse_ss_link(link):
    try:
        part = link[5:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "SS-Node"
        if "@" in part:
            user_info_b64, server_port = part.rsplit("@", 1)
            padding = 4 - (len(user_info_b64) % 4)
            if padding < 4:
                user_info_b64 += "=" * padding
            try:
                user_info = base64.b64decode(user_info_b64).decode("utf-8", errors="ignore")
            except Exception:
                user_info = user_info_b64
            if ":" in user_info:
                cipher, password = user_info.split(":", 1)
            else:
                return None
            if ":" in server_port:
                server, port = server_port.rsplit(":", 1)
                return {
                    "name": name,
                    "type": "ss",
                    "server": server,
                    "port": int(port),
                    "cipher": cipher,
                    "password": password,
                }
    except Exception:
        return None
    return None


def parse_trojan_link(link):
    try:
        part = link[9:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "Trojan-Node"
        if "@" in part:
            password, server_port = part.split("@", 1)
            if "?" in server_port:
                server_port, query = server_port.split("?", 1)
            else:
                query = ""
            if ":" in server_port:
                server, port = server_port.split(":", 1)
                sni = server
                if query:
                    parsed_q = urllib.parse.parse_qs(query)
                    if "sni" in parsed_q:
                        sni = parsed_q["sni"][0]
                return {
                    "name": name,
                    "type": "trojan",
                    "server": server,
                    "port": int(port),
                    "password": password,
                    "sni": sni,
                    "skip-cert-verify": True,
                }
    except Exception:
        return None
    return None


def parse_ssr_link(link):
    try:
        part = link[6:]
        if "#" in part:
            part, name = part.split("#", 1)
            name = urllib.parse.unquote(name)
        else:
            name = "SSR-Node"
        decoded = base64.b64decode(part + "=" * (-len(part) % 4)).decode("utf-8", errors="ignore")
        if ":" not in decoded:
            return None
        _proto, remaining = decoded.split(":", 1)
        if "/" in remaining:
            remaining = remaining.split("/", 1)[0]
        if ":" in remaining:
            server, port = remaining.rsplit(":", 1)
            return {
                "name": name,
                "type": "ssr",
                "server": server,
                "port": int(port),
                "cipher": _proto,
                "password": "",
            }
    except Exception:
        return None
    return None


def extract_proxies_from_content(text):
    proxies = []
    text_stripped = (text or "").strip()

    try:
        data = yaml.safe_load(text_stripped)
        if data and isinstance(data, dict) and "proxies" in data and isinstance(data["proxies"], list):
            proxies.extend(data["proxies"])
    except Exception:
        pass

    raw_text = text_stripped
    try:
        if not text_stripped.startswith("http") and len(text_stripped) > 20:
            padding = 4 - (len(text_stripped) % 4)
            if padding < 4:
                text_padded = text_stripped + "=" * padding
            else:
                text_padded = text_stripped
            decoded_bytes = base64.b64decode(text_padded)
            raw_text = decoded_bytes.decode("utf-8", errors="ignore")
    except Exception:
        pass

    for line in raw_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        proxy = None
        if line.startswith("vmess://"):
            proxy = parse_vmess_link(line)
        elif line.startswith("ss://"):
            proxy = parse_ss_link(line)
        elif line.startswith("trojan://"):
            proxy = parse_trojan_link(line)
        elif line.startswith("ssr://"):
            proxy = parse_ssr_link(line)
        if proxy and proxy.get("server") and proxy.get("port"):
            proxies.append(proxy)

    return deduplicate_proxies(proxies)


# ==========================================================
def generate_advanced_clash_config(valid_proxies, country_groups):
    country_group_names = list(country_groups.keys())
    seen_names = set()
    all_proxy_names = []
    for p in valid_proxies:
        name = make_unique_proxy_name(p, seen_names)
        all_proxy_names.append(name)

    proxy_groups = [
        {
            "name": "🚀 节点选择",
            "type": "select",
            "proxies": ["♻️ 自动选择"] + country_group_names + ["🎯 全球直连", "🌍 国外媒体"],
        },
        {
            "name": "♻️ 自动选择",
            "type": "url-test",
            "proxies": all_proxy_names,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        },
    ]

    for group_name, proxies in country_groups.items():
        group_names = []
        group_seen = set()
        for proxy in proxies:
            if proxy not in group_seen:
                group_names.append(proxy)
                group_seen.add(proxy)
        proxy_groups.append({
            "name": group_name,
            "type": "url-test",
            "proxies": group_names,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50,
        })

    streaming_groups = [
        {"name": "🎬 奈飞视频", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "📺 YouTube", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "✈️ 电报消息", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🌍 国外媒体", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🍎 苹果服务", "type": "select", "proxies": ["🎯 全球直连", "♻️ 自动选择"]},
        {"name": "🎯 全球直连", "type": "select", "proxies": ["DIRECT"]},
    ]
    proxy_groups.extend(streaming_groups)

    rules = [
        "DOMAIN-SUFFIX,netflix.com,🎬 奈飞视频",
        "DOMAIN-KEYWORD,netflix,🎬 奈飞视频",
        "DOMAIN-SUFFIX,youtube.com,📺 YouTube",
        "DOMAIN-KEYWORD,youtube,📺 YouTube",
        "DOMAIN-SUFFIX,t.me,✈️ 电报消息",
        "DOMAIN-SUFFIX,telegram.org,✈️ 电报消息",
        "IP-CIDR,91.108.4.0/22,✈️ 电报消息,no-resolve",
        "DOMAIN-SUFFIX,apple.com,🍎 苹果服务",
        "DOMAIN-SUFFIX,icloud.com,🍎 苹果服务",
        "DOMAIN-SUFFIX,google.com,🌍 国外媒体",
        "DOMAIN-SUFFIX,github.com,🌍 国外媒体",
        "GEOIP,CN,🎯 全球直连",
        "MATCH,🚀 节点选择",
    ]

    return {
        "proxies": valid_proxies,
        "proxy-groups": proxy_groups,
        "rules": rules,
    }


def vmess_to_link(p):
    config = {
        "v": "2",
        "ps": p.get("name", "Vmess"),
        "add": p.get("server"),
        "port": str(p.get("port")),
        "id": p.get("uuid"),
        "aid": str(p.get("alterId", 0)),
        "net": p.get("network", "tcp"),
        "type": "none",
        "host": p.get("ws-opts", {}).get("headers", {}).get("Host", "") if p.get("network") == "ws" else "",
        "path": p.get("ws-opts", {}).get("path", "") if p.get("network") == "ws" else "",
        "tls": "tls" if p.get("tls") else "",
    }
    return f"vmess://{base64.b64encode(json.dumps(config, ensure_ascii=False).encode('utf-8')).decode('utf-8')}"


def ss_to_link(p):
    user_info = f"{p.get('cipher')}:{p.get('password')}"
    b64_user = base64.b64encode(user_info.encode("utf-8")).decode("utf-8").strip("=")
    return f"ss://{b64_user}@{p.get('server')}:{p.get('port')}#{urllib.parse.quote(p.get('name', 'SS'))}"


def trojan_to_link(p):
    sni = p.get("sni", "")
    params = f"?sni={sni}&allowInsecure=1" if sni else "?allowInsecure=1"
    return f"trojan://{p.get('password')}@{p.get('server')}:{p.get('port')}{params}#{urllib.parse.quote(p.get('name', 'Trojan'))}"


def generate_v2ray_subscription(valid_proxies, output_path="sub/v2ray.txt"):
    links = []
    for p in valid_proxies:
        ptype = p.get("type")
        try:
            if ptype == "vmess":
                links.append(vmess_to_link(p))
            elif ptype == "ss":
                links.append(ss_to_link(p))
            elif ptype == "trojan":
                links.append(trojan_to_link(p))
        except Exception:
            pass
    if links:
        raw = "\n".join(links)
        b64 = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(b64)


def send_notification(send_key, total_count, final_count, mode_label):
    if not send_key:
        print("ℹ️ 未检测到 SEND_KEY，跳过微信推送。")
        return

    repo = os.environ.get("GITHUB_REPOSITORY", "username/repo")
    parts = repo.split("/")
    pages_url = f"https://{parts[0]}.github.io/{parts[1] if len(parts) > 1 else 'repo'}"
    clash_url = f"{pages_url}/sub/clash.yaml"
    v2ray_url = f"{pages_url}/sub/v2ray.txt"

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    desp = (
        "### 🟢 免费节点自动更新报告\n"
        f"- **抓取到的节点**: `{total_count}` 个\n"
        f"- **实际推送的节点**: `{final_count}` 个\n"
        f"- **模式**: `{mode_label}`\n\n"
        "---\n\n"
        "#### 📌 Clash 订阅直链\n"
        "```text\n"
        f"{clash_url}\n"
        "```\n\n"
        "#### 📌 V2Ray 订阅直链\n"
        "```text\n"
        f"{v2ray_url}\n"
        "```"
    )

    params = {
        "title": f"Free Nodes: {final_count} Nodes ({mode_label})",
        "desp": desp,
    }

    try:
        res = requests.post(url, params=params, timeout=10)
        print("📨 Server酱推送结果:", res.text)
    except Exception as e:
        print(f"⚠️ 推送通知失败: {e}")


def main():
    try:
        failure_stats = load_failure_stats()
        removed_sources = []

        if not os.path.exists("123.txt"):
            with open("123.txt", "w", encoding="utf-8") as f:
                f.write("")

        source_urls = []
        source_urls.extend([
            "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/clash.yml",
            "https://raw.githubusercontent.com/anaer/Sub/main/clash.yaml",
        ])

        for source in load_sources_from_file("123.txt"):
            if source not in source_urls:
                source_urls.append(source)

        session = requests.Session()
        session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        })

        auto_paste_sources = auto_discover_source_urls(session)
        for url in auto_paste_sources:
            if url not in source_urls:
                source_urls.append(url)
            append_unique_source(url, "123.txt")

        if auto_paste_sources:
            print(f"🔍 自动发现 paste.to 订阅源: {len(auto_paste_sources)} 个")

        source_urls = list(dict.fromkeys(source_urls))
        merged_proxies = []
        source_health_report = []

        for url in source_urls:
            print(f"\n📥 正在检查并抓取源: {url}")
            start_t = time.time()
            source_info = {"url": url, "status": "未知", "nodes": 0, "latency": 0}
            is_success = False

            try:
                res = session.get(url, timeout=15)
                source_info["latency"] = round((time.time() - start_t) * 1000, 2)

                if res.status_code == 200:
                    extracted = extract_proxies_from_content(res.text)
                    if extracted:
                        node_count = len(extracted)
                        merged_proxies.extend(extracted)
                        source_info["status"] = "🟢 正常可用"
                        source_info["nodes"] = node_count
                        is_success = True
                        print(f"✅ [健康] 解析成功 | 耗时: {source_info['latency']}ms | 有效节点: {node_count}个")
                    else:
                        if "paste.to" in url.lower():
                            discovered = discover_paste_to_sources(url, session)
                            for found_url in discovered:
                                if append_unique_source(found_url, "123.txt"):
                                    print(f"🧩 自动添加 paste.to 解析出的新订阅源: {found_url}")
                            extracted = extract_proxies_from_content(res.text)
                            if not extracted:
                                source_info["status"] = "🟡 解析无有效节点"
                                print("⚠️ [警告] 链接可达但未解析出有效节点")
                                if discovered:
                                    source_info["status"] = "🟢 paste.to 发现新订阅链接"
                        else:
                            source_info["status"] = "🟡 解析无有效节点"
                            print("⚠️ [警告] 链接可达但未解析出有效节点")
                else:
                    source_info["status"] = f"🔴 HTTP失效({res.status_code})"
                    print(f"❌ [失效] 状态码异常: {res.status_code}")

            except Exception as e:
                source_info["status"] = "⏱️ 连接超时或解析失败"
                print(f"⏱️ [错误] 抓取异常: {e}")

            current_fails = failure_stats.get(url, 0)
            if is_success:
                failure_stats[url] = 0
            else:
                current_fails += 1
                failure_stats[url] = current_fails
                print(f"⚠️ [警告] 该源连续失败次数: {current_fails}/{MAX_FAIL_COUNT}")
                if current_fails >= MAX_FAIL_COUNT:
                    print(f"🚨 [淘汰] 订阅源 {url} 连续失败超过 {MAX_FAIL_COUNT} 次，将自动剔除！")
                    removed_sources.append(url)
                    failure_stats.pop(url, None)

            source_health_report.append(source_info)

        save_failure_stats(failure_stats)
        if removed_sources:
            update_123_txt_file(removed_sources)

        print("\n" + "=" * 40)
        print("📊 订阅源健康检查汇总报告")
        print("=" * 40)
        for item in source_health_report:
            print(f"• 🔗 链接: {item['url']}")
            print(f"  状态: {item['status']} | 耗时: {item['latency']}ms | 节点数: {item['nodes']}")
        print("=" * 40 + "\n")

        merged_proxies = deduplicate_proxies(merged_proxies)
        if not merged_proxies:
            print("❌ 未能获取到任何有效原始节点。")
            return

        print(f"🌐 聚合后共有原始节点 {len(merged_proxies)} 个，开始批量获取地理位置...")
        proxies_with_geo = batch_get_locations(merged_proxies)

        print("⚡ 开始并发 TCP 测速（仅用于观测，不用于默认过滤）...")

        def check_node(proxy):
            server = proxy.get("server")
            port = proxy.get("port")
            if not server or not port:
                return None
            latency = test_tcp_latency(server, port)
            if latency is not None:
                proxy["latency"] = latency
                flag = proxy.get("geo_flag", "🌍")
                country = proxy.get("geo_country", "未知")
                proxy["name"] = f"{flag} [{country}] {proxy.get('name', 'Node')} | {latency}ms"
            else:
                proxy["latency"] = None
                flag = proxy.get("geo_flag", "🌍")
                country = proxy.get("geo_country", "未知")
                proxy["name"] = f"{flag} [{country}] {proxy.get('name', 'Node')} | 未测速"
            return proxy

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_THREAD_WORKERS) as executor:
            results = list(executor.map(check_node, proxies_with_geo))

        all_proxies = [p for p in results if p is not None]
        all_proxies.sort(key=lambda x: (x.get("latency") is None, x.get("latency") or 999999, x.get("geo_country") == "其他"))
        print(f"🎯 总抓取后保留节点数: {len(all_proxies)}")

        final_proxies = select_final_proxies(all_proxies)
        print(f"📦 实际输出节点数: {len(final_proxies)}")

        if not final_proxies:
            print("❌ 没有可输出节点，已中止生成订阅。")
            return

        seen_names = set()
        for p in final_proxies:
            make_unique_proxy_name(p, seen_names)

        country_groups = {}
        for p in final_proxies:
            group_name = f"{p.get('geo_flag')} {p.get('geo_country')} 节点"
            country_groups.setdefault(group_name, []).append(p["name"])

        clash_config = generate_advanced_clash_config(final_proxies, country_groups)

        for p in clash_config["proxies"]:
            p.pop("latency", None)
            p.pop("geo_country", None)
            p.pop("geo_flag", None)

        os.makedirs("sub", exist_ok=True)
        with open("sub/clash.yaml", "w", encoding="utf-8") as f:
            yaml.dump(clash_config, f, allow_unicode=True, sort_keys=False)
        print("💾 Clash 文件已写入 sub/clash.yaml")

        generate_v2ray_subscription(final_proxies, "sub/v2ray.txt")
        print("💾 V2Ray 文件已写入 sub/v2ray.txt")

        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name == "workflow_dispatch":
            mode_label = "strict" if STRICT_FILTER_MODE else "full"
            send_notification(os.environ.get("SEND_KEY"), len(all_proxies), len(final_proxies), mode_label)
        else:
            print("ℹ️ 当前为定时自动运行，已跳过 Server酱微信推送。")

        print("🎉 执行完毕！")

    except Exception as e:
        print(f"❌ 运行发生错误: {e}")
        raise


if __name__ == "__main__":
    main()
