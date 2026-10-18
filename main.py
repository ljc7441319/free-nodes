import os
import socket
import time
import concurrent.futures
import base64
import json
import urllib.parse
import requests
import yaml

# 失败统计配置：用于跨多次运行自动记录并淘汰长期失效的源
STATS_FILE = "source_stats.json"
MAX_FAIL_COUNT = 3  # 连续失败达到 3 次后自动从 123.txt 中剔除

def load_failure_stats():
    if os.path.exists(STATS_FILE):
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_failure_stats(stats):
    try:
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(stats, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 保存统计状态异常: {e}")

def update_123_txt_file(removed_urls):
    """当源连续失败超限时，自动从 123.txt 中移除该行"""
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

def batch_get_locations(proxies):
    queries = []
    ip_to_proxy_indices = {}
    
    for idx, proxy in enumerate(proxies):
        server = proxy.get('server')
        if not server:
            continue
        try:
            ip = socket.gethostbyname(server)
            queries.append({"query": ip, "fields": "status,country,countryCode"})
            ip_to_proxy_indices.setdefault(ip, []).append(idx)
        except Exception:
            continue

    if not queries:
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
                        proxies[idx]['geo_country'] = country
                        proxies[idx]['geo_flag'] = flag
    except Exception as e:
        print(f"⚠️ 批量查询地理位置异常: {e}")

    for proxy in proxies:
        if 'geo_country' not in proxy:
            proxy['geo_country'] = "其他"
            proxy['geo_flag'] = "🌍"

    return proxies

def test_tcp_latency(server, port, timeout=3):
    start_time = time.time()
    try:
        with socket.create_connection((server, int(port)), timeout=timeout):
            latency = (time.time() - start_time) * 1000
            return round(latency, 2)
    except Exception:
        return None

def generate_advanced_clash_config(valid_proxies, country_groups):
    country_group_names = list(country_groups.keys())
    all_proxy_names = [p['name'] for p in valid_proxies]

    proxy_groups = [
        {
            "name": "🚀 节点选择",
            "type": "select",
            "proxies": ["♻️ 自动选择"] + country_group_names + ["🎯 全球直连", "🌍 国外媒体"]
        },
        {
            "name": "♻️ 自动选择",
            "type": "url-test",
            "proxies": all_proxy_names,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50
        }
    ]

    for group_name, proxies in country_groups.items():
        proxy_groups.append({
            "name": group_name,
            "type": "url-test",
            "proxies": proxies,
            "url": "http://www.gstatic.com/generate_204",
            "interval": 300,
            "tolerance": 50
        })

    # 已彻底解耦嵌套，修复 loop is detected in ProxyGroup 错误
    streaming_groups = [
        {"name": "🎬 奈飞视频", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "📺 YouTube", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "✈️ 电报消息", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🌍 国外媒体", "type": "select", "proxies": ["♻️ 自动选择"] + country_group_names},
        {"name": "🍎 苹果服务", "type": "select", "proxies": ["🎯 全球直连", "♻️ 自动选择"]},
        {"name": "🎯 全球直连", "type": "select", "proxies": ["DIRECT"]}
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
        "MATCH,🚀 节点选择"
    ]

    return {
        "proxies": valid_proxies,
        "proxy-groups": proxy_groups,
        "rules": rules
    }

def vmess_to_link(p):
    config = {
        "v": "2", "ps": p.get('name', 'Vmess'), "add": p.get('server'),
        "port": str(p.get('port')), "id": p.get('uuid'), "aid": str(p.get('alterId', 0)),
        "net": p.get('network', 'tcp'), "type": "none",
        "host": p.get('ws-opts', {}).get('headers', {}).get('Host', '') if p.get('network') == 'ws' else '',
        "path": p.get('ws-opts', {}).get('path', '') if p.get('network') == 'ws' else '',
        "tls": "tls" if p.get('tls') else ""
    }
    return f"vmess://{base64.b64encode(json.dumps(config, ensure_ascii=False).encode('utf-8')).decode('utf-8')}"

def ss_to_link(p):
    user_info = f"{p.get('cipher')}:{p.get('password')}"
    b64_user = base64.b64encode(user_info.encode('utf-8')).decode('utf-8').strip('=')
    return f"ss://{b64_user}@{p.get('server')}@{p.get('port')}#{urllib.parse.quote(p.get('name', 'SS'))}"

def trojan_to_link(p):
    sni = p.get('sni', '')
    params = f"?sni={sni}&allowInsecure=1" if sni else "?allowInsecure=1"
    return f"trojan://{p.get('password')}@{p.get('server')}:{p.get('port')}{params}#{urllib.parse.quote(p.get('name', 'Trojan'))}"

def generate_v2ray_subscription(valid_proxies, output_path="sub/v2ray.txt"):
    links = []
    for p in valid_proxies:
        ptype = p.get('type')
        try:
            if ptype == 'vmess': links.append(vmess_to_link(p))
            elif ptype == 'ss': links.append(ss_to_link(p))
            elif ptype == 'trojan': links.append(trojan_to_link(p))
        except Exception:
            pass
    if links:
        raw = "\n".join(links)
        b64 = base64.b64encode(raw.encode('utf-8')).decode('utf-8')
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(b64)

def send_notification(send_key, success_count):
    if not send_key: 
        print("ℹ️ 未检测到 SEND_KEY，跳过微信推送。")
        return
    repo = os.environ.get("GITHUB_REPOSITORY", "username/repo")
    parts = repo.split("/")
    pages_url = f"https://{parts[0]}.github.io/{parts[1] if len(parts)>1 else 'repo'}"
    
    clash_url = f"{pages_url}/sub/clash.yaml"
    v2ray_url = f"{pages_url}/sub/v2ray.txt"

    url = f"https://sctapi.ftqq.com/{send_key}.send"
    
    desp = (
        "### 🟢 免费节点自动更新报告\n"
        f"- **有效可用节点数**: `{success_count}` 个\n"
        "- **触发方式**: 手动触发推送\n\n"
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
        "title": "Free Nodes Manual Update Success",
        "desp": desp
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

        # 默认基础源
        source_urls = [
            "https://raw.githubusercontent.com/ermaozi/get_subscribe/main/subscribe/clash.yml",
            "https://raw.githubusercontent.com/anaer/Sub/main/clash.yaml"
        ]
        
        # 自动读取 123.txt 中的扩展外部订阅源
        if os.path.exists("123.txt"):
            try:
                with open("123.txt", "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            if line not in source_urls:
                                source_urls.append(line)
                print(f"📄 已成功加载 123.txt 外部源，待检查总源数: {len(source_urls)}")
            except Exception as e:
                print(f"⚠️ 读取 123.txt 异常: {e}")

        merged_proxies = []
        source_health_report = []

        for url in source_urls:
            print(f"\n📥 正在检查并抓取源: {url}")
            start_t = time.time()
            source_info = {"url": url, "status": "未知", "nodes": 0, "latency": 0}
            is_success = False
            
            try:
                res = requests.get(url, timeout=15)
                source_info["latency"] = round((time.time() - start_t) * 1000, 2)
                
                if res.status_code == 200:
                    data = yaml.safe_load(res.text)
                    if data and isinstance(data, dict) and 'proxies' in data:
                        node_count = len(data['proxies'])
                        merged_proxies.extend(data['proxies'])
                        source_info["status"] = "🟢 正常可用"
                        source_info["nodes"] = node_count
                        is_success = True
                        print(f"✅ [健康] 抓取成功 | 耗时: {source_info['latency']}ms | 贡献有效节点: {node_count}个")
                    else:
                        source_info["status"] = "🟡 格式异常(无proxies字段)"
                        print(f"⚠️ [警告] 格式错误: 未检测到 clash 节点列表")
                else:
                    source_info["status"] = f"🔴 HTTP失效({res.status_code})"
                    print(f"❌ [失效] 状态码异常: {res.status_code}")
                    
            except Exception as e:
                source_info["status"] = "⏱️ 连接超时或解析失败"
                print(f"⏱️ [错误] 抓取异常: {e}")
            
            # 失败统计与自动淘汰机制
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

        print("\n" + "="*40)
        print("📊 订阅源健康检查汇总报告")
        print("="*40)
        for item in source_health_report:
            print(f"• 🔗 链接: {item['url']}")
            print(f"  状态: {item['status']} | 耗时: {item['latency']}ms | 节点数: {item['nodes']}")
        print("="*40 + "\n")

        if not merged_proxies:
            print("❌ 未能获取到任何有效原始节点。")
            return

        print(f"🌐 共有原始节点 {len(merged_proxies)} 个，开始批量获取地理位置...")
        proxies_with_geo = batch_get_locations(merged_proxies)

        print("⚡ 开始并发 TCP 测速...")
        def check_node(proxy):
            server = proxy.get('server')
            port = proxy.get('port')
            if not server or not port: return None
            latency = test_tcp_latency(server, port)
            if latency is not None:
                proxy['latency'] = latency
                flag = proxy.get('geo_flag', '🌍')
                country = proxy.get('geo_country', '未知')
                proxy['name'] = f"{flag} [{country}] {proxy.get('name', 'Node')} | {latency}ms"
                return proxy
            return None

        with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
            results = list(executor.map(check_node, proxies_with_geo))

        valid_proxies = [p for p in results if p is not None]
        valid_proxies.sort(key=lambda x: x['latency'])
        print(f"🎯 测速完成，存活可用节点数: {len(valid_proxies)}")

        if not valid_proxies:
            print("❌ 所有节点测速均失败。")
            return

        country_groups = {}
        for p in valid_proxies:
            group_name = f"{p.get('geo_flag')} {p.get('geo_country')} 节点"
            country_groups.setdefault(group_name, []).append(p['name'])

        clash_config = generate_advanced_clash_config(valid_proxies, country_groups)

        for p in clash_config['proxies']:
            p.pop('latency', None)
            p.pop('geo_country', None)
            p.pop('geo_flag', None)

        os.makedirs("sub", exist_ok=True)
        with open("sub/clash.yaml", "w", encoding="utf-8") as f:
            yaml.dump(clash_config, f, allow_unicode=True, sort_keys=False)
        print("💾 Clash 文件已写入 sub/clash.yaml")

        generate_v2ray_subscription(valid_proxies, "sub/v2ray.txt")
        print("💾 V2Ray 文件已写入 sub/v2ray.txt")

        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name == "workflow_dispatch":
            send_notification(os.environ.get("SEND_KEY"), len(valid_proxies))
        else:
            print("ℹ️ 当前为定时自动运行，已跳过 Server酱微信推送。")

        print("🎉 执行完毕！")

    except Exception as e:
        print(f"❌ 运行发生错误: {e}")
        exit(1)

if __name__ == "__main__":
    main()
