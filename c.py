from DrissionPage import ChromiumPage, ChromiumOptions
import time
import csv
import json
from urllib.parse import parse_qs, urlencode

# ===== 浏览器配置 =====
co = ChromiumOptions()
co.set_browser_path(r'c:\Program Files\Google\Chrome\Application\chrome.exe')
co.set_local_port(9333)
co.set_user_data_path(r'D:\chrome_debug_profile')

dp = ChromiumPage(co)

# ===== 第一步：打开页面，点评论，抓第一个真实请求 =====
dp.listen.start('client.action')
dp.get('https://item.jd.com/10127955410850.html')
time.sleep(3)

# 点全部评价
for sel in ['text=全部评价', 'text=全部', '.comment-filter-item']:
    try:
        btn = dp.ele(sel, timeout=3)
        if btn:
            btn.scroll.to_see()
            time.sleep(1)
            btn.click()
            print(f"✅ 点击按钮: {sel}")
            break
    except:
        continue

# 等第一个数据包，提取请求参数模板
print("⏳ 等待第一个评论数据包...")
template_params = None
for _ in range(20):
    resp = dp.listen.wait(timeout=10)
    if resp is None:
        break
    if not hasattr(resp, 'response') or resp.response is None:
        continue
    body = resp.response.body
    if isinstance(body, dict) and 'result' in body:
        req = resp.request
        template_params = {
            'url': req.url,
            'headers': dict(req.headers),
            'postData': req.postData
        }
        print(f"✅ 抓到模板请求！")
        print(f"   URL: {template_params['url']}")
        break

dp.listen.stop()

if not template_params:
    print("❌ 未抓到模板请求，程序退出")
    exit()

# ===== 第二步：解析 postData，提取 body 参数 =====
post_dict = parse_qs(template_params['postData'])
post_single = {k: v[0] for k, v in post_dict.items()}
body_json = json.loads(post_single['body'])
print(f"   当前 pageNum : {body_json.get('pageNum')}")
print(f"   sku         : {body_json.get('sku')}")

# ===== 第三步：定义翻页请求函数 =====
def fetch_page(page_num):
    body_json['pageNum']        = str(page_num)
    body_json['pageSize']       = "20"
    body_json['isFirstRequest'] = "false"
    post_single['body'] = json.dumps(body_json, ensure_ascii=False)
    post_data_str = urlencode(post_single)

    js_code = """
    return new Promise((resolve) => {
        fetch("%s", {
            method: "POST",
            headers: {
                "Content-Type": "application/x-www-form-urlencoded",
                "Referer": "https://item.jd.com/"
            },
            body: %s,
            credentials: "include"
        })
        .then(r => r.json())
        .then(data => resolve(JSON.stringify(data)))
        .catch(e  => resolve("ERROR:" + e.toString()));
    });
    """ % (template_params['url'], json.dumps(post_data_str))

    return dp.run_js(js_code, as_expr=False)


# ===== 第四步：定义解析函数 =====
seen_keys = set()
total     = 0
# ⬇️ 改用列表收集所有评论
all_reviews = []

def find_comment_list(obj):
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and 'commentInfo' in obj[0]:
            return obj
        for item in obj:
            result = find_comment_list(item)
            if result:
                return result
    elif isinstance(obj, dict):
        for v in obj.values():
            result = find_comment_list(v)
            if result:
                return result
    return None

def parse_and_collect(raw_json_str, page_label):
    global total

    try:
        data = json.loads(raw_json_str)
    except Exception as e:
        print(f"  [{page_label}] JSON 解析失败: {e}")
        return -1

    if str(data.get('code')) != '0':
        print(f"  [{page_label}] 接口异常: code={data.get('code')}")
        return -1

    try:
        datas = data['result']['floors'][2]['data']
        if not datas or 'commentInfo' not in str(datas[0]):
            raise ValueError("路径内容不是评论")
    except Exception:
        datas = find_comment_list(data)

    if not datas:
        print(f"  [{page_label}] 未找到评论数据")
        return 0

    count = 0
    for item in datas:
        try:
            info = item['commentInfo']
            key  = info['userNickName'] + info['commentDate'] + info.get('commentData', '')[:10]
            if key in seen_keys:
                continue
            seen_keys.add(key)
            # ⬇️ 直接 append 到列表，格式对应你的 JSON 样本
            all_reviews.append({
                "review_content": info.get('commentData', ''),
                "rating":         int(info['commentScore']) if info.get('commentScore') else 0,
                "review_time":    info.get('commentDate', ''),
                "product_model":  info.get('productSpecifications', ''),
                "likes":          int(info['buyCount']) if info.get('buyCount') else 0
            })
            count += 1
        except (KeyError, TypeError):
            continue

    total += count
    print(f"  ✅ [{page_label}] 本页新增 {count} 条 | 累计 {total} 条")
    return count


# ===== 第五步：循环翻页爬取 =====
max_pages       = 300
max_empty_pages = 5
empty_count     = 0

print(f"\n{'='*45}")
print("🚀 开始直接 API 翻页爬取（每页20条，间隔0.5秒）")
print(f"{'='*45}\n")

try:
    for page in range(1, max_pages + 1):
        print(f"📄 第 {page} 页...", end="  ")

        raw = fetch_page(page)

        if raw is None or str(raw).startswith("ERROR"):
            print(f"请求失败: {raw}")
            empty_count += 1
        else:
            result = parse_and_collect(raw, f"第{page}页")
            if result > 0:
                empty_count = 0
            else:
                empty_count += 1
                print(f"  ⚠️ 连续无新数据 {empty_count} 次")

        if empty_count >= max_empty_pages:
            print(f"\n✅ 连续 {max_empty_pages} 页无新数据，判定已到底！")
            break

        time.sleep(0.5)

except KeyboardInterrupt:
    print("\n⚠️ 用户手动中断")

finally:
    output_file = '评论.json'
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump({"reviews": all_reviews}, f, ensure_ascii=False, indent=2)
    
    print(f"\n🎉 爬取完成！共写入 {total} 条评论 → {output_file}")