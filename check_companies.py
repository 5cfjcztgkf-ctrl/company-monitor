#!/usr/bin/env python3
"""
企业宣讲会监控 - 四路情报系统
===================================
策略1: 微信公众平台 → DuckDuckGo/Bing 搜索 mp.weixin.qq.com 文章
策略2: 公司校招官网 → 华为 career.huawei.com / 长鑫存储 cxmt.com
策略3: 第三方平台   → 牛客网 nowcoder.com / 应届生 yingjiesheng.com
策略4: 各校就业网   → 直接抓取就业信息网宣讲会页面

运行环境: GitHub Actions (ubuntu-latest)
依赖: pip install requests beautifulsoup4
"""

import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin, quote

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("请先安装依赖: pip install requests beautifulsoup4")
    sys.exit(1)

# ============================================================
# 配置
# ============================================================

DATA_FILE = os.path.join(os.path.dirname(__file__), "data", "companies.json")
TZ = timezone(timedelta(hours=8))
REQUEST_TIMEOUT = 15
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# 目标公司 → 简称映射
COMPANY_MAP = {"华为": "huawei", "长鑫存储": "cxmt"}

# 日期正则
DATE_PATTERNS = [
    (r'(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})', '{}-{:02d}-{:02d}'),
    (r'(\d{4})年(\d{1,2})月(\d{1,2})日', '{}-{:02d}-{:02d}'),
    (r'(\d{1,2})月(\d{1,2})日', None),
]

# 排除词
EXCLUDE_KEYWORDS = ["实习", "提前批", "内推", "测评", "笔试", "面试经验", "面经"]


# ============================================================
# HTTP 工具
# ============================================================

def fetch_page(url, encoding=None, method="GET", headers_extra=None, data=None):
    """抓取网页，返回 (html, soup, error)"""
    headers = {"User-Agent": USER_AGENT}
    if headers_extra:
        headers.update(headers_extra)

    try:
        if method == "POST":
            resp = requests.post(url, headers=headers, timeout=REQUEST_TIMEOUT,
                               allow_redirects=True, verify=False, data=data)
        else:
            resp = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT,
                              allow_redirects=True, verify=False)
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        return None, None, "timeout"
    except requests.exceptions.ConnectionError:
        return None, None, "connection_error"
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response else "unknown"
        return None, None, f"http_error:{code}"
    except Exception as e:
        return None, None, f"error:{str(e)[:80]}"

    # 编码检测
    if encoding:
        resp.encoding = encoding
    elif resp.apparent_encoding:
        resp.encoding = resp.apparent_encoding
    else:
        for enc in ["utf-8", "gb2312", "gbk", "gb18030"]:
            try:
                resp.content.decode(enc)
                resp.encoding = enc
                break
            except Exception:
                continue

    try:
        html = resp.text
    except Exception:
        return None, None, "decode_error"

    soup = BeautifulSoup(html, "html.parser")
    return html, soup, None


# ============================================================
# 信息提取
# ============================================================

def extract_dates(text):
    """提取日期 → [(原始文本, 标准化日期), ...]"""
    results = []
    for pattern, fmt in DATE_PATTERNS:
        for m in re.finditer(pattern, text):
            if fmt:
                try:
                    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    if 2025 <= y <= 2027 and 1 <= mo <= 12 and 1 <= d <= 31:
                        results.append((m.group(0), fmt.format(y, mo, d)))
                except Exception:
                    continue
            else:
                try:
                    mo, d = int(m.group(1)), int(m.group(2))
                    if 1 <= mo <= 12 and 1 <= d <= 31:
                        results.append((m.group(0), f"2026-{mo:02d}-{d:02d}"))
                except Exception:
                    continue
    return results


def extract_venue(text):
    """提取地点"""
    patterns = [
        r'地点[：:]\s*(.{2,30}?)(?:$|\n|\r|\s{2,})',
        r'场地[：:]\s*(.{2,30}?)(?:$|\n|\r|\s{2,})',
        r'地址[：:]\s*(.{2,30}?)(?:$|\n|\r|\s{2,})',
        r'([\w\u4e00-\u9fff]+(?:教室|报告厅|会议室|大厅|中心|楼\d+|馆|堂|招聘))',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            venue = m.group(1).strip()
            if 2 <= len(venue) <= 50:
                return venue
    return ""


def extract_time(text):
    """提取时间"""
    patterns = [
        r'时间[：:]\s*(.{3,20}?)(?:$|\n|\r|\s{2,})',
        r'(\d{1,2}:\d{2}\s*[-~—至到]\s*\d{1,2}:\d{2})',
        r'(\d{1,2}:\d{2})',
        r'([上下中]午\d{1,2}[时点])',
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1).strip()
    return ""


def is_school_related(text, school_names):
    """检查文本是否与目标学校相关"""
    for sn in school_names:
        if sn in text:
            return True
    return False


def make_event(date_str, time_str, venue, source_url, source_label, notes):
    """构建标准事件对象"""
    return {
        "date": date_str,
        "time": time_str,
        "venue": venue,
        "source_url": source_url,
        "source_label": source_label,
        "type": "宣讲会",
        "notes": notes[:100] if notes else "",
    }


def merge_events(all_events):
    """合并去重多个来源的事件，按日期排序"""
    seen = set()
    unique = []
    for e in all_events:
        key = (e["date"], e["venue"], e["time"])
        if key not in seen and e["date"]:
            seen.add(key)
            unique.append(e)
    unique.sort(key=lambda x: x["date"])
    return unique[:8]


# ============================================================
# 策略1: 微信公众平台 (通过 DuckDuckGo 搜索)
# ============================================================

def strategy_1_wechat(univ_name, short_name, company_name):
    """通过 DuckDuckGo 搜索微信公众号文章"""
    query = f"site:mp.weixin.qq.com {company_name} {univ_name} 宣讲会 2027"
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}"
    print(f"     [策略1-微信] 搜索: {company_name} {short_name}...", end=" ")

    html, soup, error = fetch_page(url)
    if error:
        print(f"❌ {error}")
        return []

    results = soup.select(".result__body")
    events = []
    school_names = [univ_name]
    if short_name != univ_name:
        school_names.append(short_name)

    for item in results[:10]:
        link = item.select_one(".result__url")
        snippet = item.select_one(".result__snippet")
        title_el = item.select_one(".result__title a")

        if not snippet:
            continue

        text = snippet.get_text(" ", strip=True)
        # 必须同时包含公司名和学校名
        if company_name not in text:
            continue
        if not is_school_related(text, school_names):
            continue
        if any(kw in text for kw in EXCLUDE_KEYWORDS):
            continue

        dates = extract_dates(text)
        venue = extract_venue(text)
        event_time = extract_time(text)
        source_url = link.get_text(strip=True) if link else ""
        title = title_el.get_text(strip=True) if title_el else text[:80]

        for d in dates[:2]:
            events.append(make_event(d[1], event_time, venue, source_url,
                                     "微信公众号", title))

    if events:
        print(f"✅ 找到 {len(events)} 条")
    else:
        print("⚪ 无结果")
    return events


# ============================================================
# 策略2: 公司校招官网
# ============================================================

def strategy_2_career_site(univ_name, short_name, company_name):
    """抓取公司校招官网的宣讲会行程"""
    if company_name == "华为":
        return _strategy_2_huawei(univ_name, short_name)
    else:
        return _strategy_2_cxmt(univ_name, short_name)


def _strategy_2_huawei(univ_name, short_name):
    """华为校招官网"""
    # 华为校招页面是 JS 渲染的，直接用 API
    api_url = "https://career.huawei.com/reccampportal/services/portal/portaluser/queryCampusRecruitmentInfos"
    print(f"     [策略2-华为官网] 查询中...", end=" ")

    try:
        headers_extra = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        html, soup, error = fetch_page(api_url, method="POST",
                                    headers_extra=headers_extra, data="{}")
    except Exception:
        print("⚠️ API异常")
        return []

    if error or not html:
        # 回退: 尝试抓 HTML 页面
        career_url = "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html"
        print(f"(回退HTML)...", end=" ")
        html, soup, error = fetch_page(career_url)
        if error:
            print(f"❌ {error}")
            return []

    if not html:
        print("⚪ 无内容")
        return []

    events = []
    school_names = [univ_name]
    if short_name != univ_name:
        school_names.append(short_name)

    # 尝试解析 JSON
    try:
        data = json.loads(html)
        if isinstance(data, dict) and "data" in data:
            items = data["data"]
            if isinstance(items, list):
                for item in items:
                    if isinstance(item, str):
                        continue
                    city = str(item.get("city", ""))
                    school = str(item.get("school", ""))
                    date_str = str(item.get("time", "")) or str(item.get("date", ""))
                    address = str(item.get("address", "")) or str(item.get("location", ""))
                    time_str = str(item.get("timeDetail", "")) or str(item.get("specificTime", ""))

                    if not is_school_related(school + city + address, school_names):
                        continue

                    events.append(make_event(
                        date_str[:10], time_str, address or city,
                        "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html",
                        "华为校招官网", f"华为 {school} 宣讲会"
                    ))
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass

    # 回退: HTML 解析
    if not events and soup:
        body = soup.get_text(" ", strip=True)
        for sn in school_names:
            if sn in body and "华为" in body:
                dates = extract_dates(body)
                venue = extract_venue(body)
                event_time = extract_time(body)
                for d in dates[:2]:
                    events.append(make_event(
                        d[1], event_time, venue,
                        "https://career.huawei.com/reccampportal/portal5/campus-recruitment.html",
                        "华为校招官网", f"华为 {univ_name} 宣讲会"
                    ))
                break

    if events:
        print(f"✅ 找到 {len(events)} 条")
    else:
        print("⚪ 无结果")
    return events


def _strategy_2_cxmt(univ_name, short_name):
    """长鑫存储校招官网"""
    career_url = "https://www.cxmt.com/career/campus"
    print(f"     [策略2-长鑫官网] 抓取中...", end=" ")

    html, soup, error = fetch_page(career_url)
    if error:
        print(f"❌ {error}")
        return []
    if not soup:
        print("⚪ 无内容")
        return []

    events = []
    body = soup.get_text(" ", strip=True)
    school_names = [univ_name]
    if short_name != univ_name:
        school_names.append(short_name)

    for sn in school_names:
        if sn not in body:
            continue

        # 找包含学校名的上下文
        idx = body.find(sn)
        start = max(0, idx - 200)
        end = min(len(body), idx + 300)
        context = body[start:end]

        if "长鑫" not in context:
            continue
        if any(kw in context for kw in EXCLUDE_KEYWORDS):
            continue

        dates = extract_dates(context)
        venue = extract_venue(context)
        event_time = extract_time(context)

        for d in dates[:2]:
            events.append(make_event(
                d[1], event_time, venue,
                "https://www.cxmt.com/career/campus",
                "长鑫校招官网", f"长鑫存储 {sn} 宣讲会"
            ))
        break

    if events:
        print(f"✅ 找到 {len(events)} 条")
    else:
        print("⚪ 无结果")
    return events


# ============================================================
# 策略3: 第三方平台 (牛客网 / 应届生)
# ============================================================

def strategy_3_third_party(univ_name, short_name, company_name):
    """搜索牛客网和应届生求职网"""
    events = []

    # 3a. 牛客网
    nowcoder_url = f"https://www.nowcoder.com/search?type=post&query={quote(company_name + ' ' + univ_name)}"
    print(f"     [策略3-牛客] 搜索中...", end=" ")

    html, soup, error = fetch_page(nowcoder_url, headers_extra={
        "Accept": "text/html,application/xhtml+xml",
    })
    if error:
        print(f"❌ {error}", end=" ")
    elif soup:
        body = soup.get_text(" ", strip=True)
        school_names = [univ_name]
        if short_name != univ_name:
            school_names.append(short_name)

        if is_school_related(body, school_names) and company_name in body:
            dates = extract_dates(body)
            venue = extract_venue(body)
            event_time = extract_time(body)
            for d in dates[:2]:
                events.append(make_event(
                    d[1], event_time, venue, nowcoder_url,
                    "牛客网", f"[牛客] {company_name} {univ_name} 宣讲会"
                ))
            print(f"✅ {len(dates[:2])} 条", end=" ")
        else:
            print("⚪", end=" ")

    # 3b. 应届生求职网
    yjs_url = f"https://www.yingjiesheng.com/searchresult?keyword={quote(company_name)}"
    print(f"| [策略3-应届生]...", end=" ")

    html, soup, error = fetch_page(yjs_url)
    if error:
        print(f"❌ {error}")
    elif soup:
        body = soup.get_text(" ", strip=True)
        school_names = [univ_name]
        if short_name != univ_name:
            school_names.append(short_name)

        if is_school_related(body, school_names) and company_name in body:
            dates = extract_dates(body)
            venue = extract_venue(body)
            event_time = extract_time(body)
            for d in dates[:2]:
                evt = make_event(d[1], event_time, venue, yjs_url,
                                "应届生求职网", f"[应届生] {company_name} {univ_name} 宣讲会")
                if evt not in events:
                    events.append(evt)
            print(f"✅ 新增")
        else:
            print("⚪")

    return events


# ============================================================
# 策略4: 各校就业网 (原有方案)
# ============================================================

def strategy_4_employment_site(univ_name, short_name, company_name, search_url):
    """直接抓取就业信息网宣讲会页面"""
    if not search_url:
        return []

    print(f"     [策略4-就业网] 抓取中...", end=" ")
    html, soup, error = fetch_page(search_url)

    if error:
        print(f"❌ {error}")
        return []
    if not soup:
        print("⚪ 无内容")
        return []

    events = []
    body_text = soup.get_text()

    # 方法A: 找包含公司名的链接
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if company_name not in text:
            continue
        if any(kw in text for kw in EXCLUDE_KEYWORDS):
            continue
        if len(text) < 6:
            continue

        href = urljoin(search_url, a["href"])
        # 找父容器的完整文本
        container = a.parent
        for _ in range(5):
            if container is None:
                break
            ct = container.get_text(" ", strip=True)
            if len(ct) > len(text) + 10:
                break
            container = container.parent

        container_text = (container.get_text(" ", strip=True)
                        if container else text)

        dates = extract_dates(container_text)
        venue = extract_venue(container_text)
        event_time = extract_time(container_text)

        for d in dates[:2]:
            events.append(make_event(d[1], event_time, venue, href,
                                    "高校就业网", text))

    # 方法B: 文本全文搜索（适用于非链接形式的列表）
    if not events:
        lines = body_text.split("\n")
        for i, line in enumerate(lines):
            if company_name not in line:
                continue
            if any(kw in line for kw in EXCLUDE_KEYWORDS):
                continue
            if len(line) < 6:
                continue

            ctx_start = max(0, i - 3)
            ctx_end = min(len(lines), i + 4)
            context = " ".join(lines[ctx_start:ctx_end])
            dates = extract_dates(context)
            venue = extract_venue(context)
            event_time = extract_time(context)

            for d in dates[:2]:
                events.append(make_event(d[1], event_time, venue, search_url,
                                        "高校就业网", line.strip()))
            if events:
                break

    if events:
        print(f"✅ 找到 {len(events)} 条")
    else:
        print("⚪ 无结果")
    return events


# ============================================================
# 综合检查：四路并行
# ============================================================

def check_university_company(u, company_name):
    """对单个高校+公司，同时运行4个策略"""
    univ_name = u["name"]
    short_name = u.get("short_name", univ_name)
    search_url = u.get("presentation_search_url", "")

    all_events = []

    # 并行运行4个策略
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {}

        # 策略1: 微信
        f1 = executor.submit(strategy_1_wechat, univ_name, short_name, company_name)
        futures[f1] = "策略1-微信"

        # 策略2: 公司官网
        f2 = executor.submit(strategy_2_career_site, univ_name, short_name, company_name)
        futures[f2] = "策略2-公司官网"

        # 策略3: 第三方
        f3 = executor.submit(strategy_3_third_party, univ_name, short_name, company_name)
        futures[f3] = "策略3-第三方"

        # 策略4: 就业网
        f4 = executor.submit(strategy_4_employment_site,
                            univ_name, short_name, company_name, search_url)
        futures[f4] = "策略4-就业网"

        for future in as_completed(futures):
            label = futures[future]
            try:
                result = future.result(timeout=30)
                if result:
                    all_events.extend(result)
            except Exception as e:
                print(f"\n       ⚠️ {label} 异常: {str(e)[:60]}")

    # 合并去重
    final_events = merge_events(all_events)

    if final_events:
        return {"status": "scheduled", "events": final_events}
    else:
        return {"status": "not_found", "events": []}


# ============================================================
# 主流程
# ============================================================

def main():
    print("=" * 60)
    print("  企业宣讲会监控 - 四路情报系统")
    print(f"  时间: {datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')} 北京时间")
    print(f"  范围: 27所高校 × 华为+长鑫存储")
    print(f"  策略: 微信 | 公司官网 | 牛客/应届生 | 就业网")
    print("=" * 60)
    print()

    # 抑制 SSL 警告
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    # 读取数据
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    universities = data["universities"]
    total = {"华为": 0, "长鑫存储": 0}

    for i, u in enumerate(universities):
        name = u["name"]
        print(f"\n[{i+1:2d}/27] 📍 {name}")
        print(f"    {'─' * 40}")

        for company in ["华为", "长鑫存储"]:
            key = COMPANY_MAP[company]
            print(f"   🔍 {company}:")

            result = check_university_company(u, company)
            u[key] = result

            count = len(result["events"]) if result["events"] else 0
            if result["status"] == "scheduled":
                total[company] += 1
                print(f"       ✅ 共找到 {count} 条宣讲会信息:")
                for evt in result["events"][:3]:
                    src = evt.get("source_label", "?")
                    print(f"          📅 {evt['date']} {evt['time']} | 📍 {evt['venue']} | 📡 {src}")
            else:
                print(f"       ⚪ 暂未发现宣讲会信息")

        # 高校间隔
        if i < len(universities) - 1:
            time.sleep(1)

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  📊 检查完毕")
    print(f"  华为有宣讲会的高校:   {total['华为']}/27")
    print(f"  长鑫存储有宣讲会的高校: {total['长鑫存储']}/27")
    print(f"{'=' * 60}")

    # 保存
    data["last_updated"] = datetime.now(TZ).strftime("%Y-%m-%dT%H:%M:%S+08:00")

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"\n数据已保存: {DATA_FILE}")
    return total["华为"] + total["长鑫存储"]


if __name__ == "__main__":
    main()
