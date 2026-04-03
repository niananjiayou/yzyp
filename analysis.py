
import pandas as pd
import json
import os
import re
import time
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed
from wordcloud import WordCloud
import matplotlib
matplotlib.use('Agg')
import matplotlib.font_manager as fm
import numpy as np
from PIL import Image

# ══════════════════════════════════════════════════════════════════
#  言之有"品" · AI评论分析流水线 v4.1
#  完全通用版：支持任意商品品类，无硬编码品类词
#  输入: data.csv
#  输出: 最终分析报告/<产品名>/ + 结构化分析结果.json
# ══════════════════════════════════════════════════════════════════

INPUT_CSV        = "data.csv"
OUTPUT_DIR       = "最终分析报告"
MERGED_JSON_PATH = "结构化分析结果.json"
MASK_PATH        = "cloud_mask.png"

COL_CONTENT  = "review_content"
COL_RATING   = "rating"
COL_TIME     = "review_time"
COL_PRODUCT  = "product_model"
COL_LIKES    = "likes"

MAX_WORKERS  = 5
MAX_RETRIES  = 3

# ══════════════════════════════════════════════════════════════════
#  工具函数：调用GLM（带重试）
# ══════════════════════════════════════════════════════════════════

def call_glm(prompt: str, temperature: float = 0.1) -> str:
    api_key = os.getenv("ZHIPUAI_API_KEY")
    if not api_key:
        return "__NO_KEY__"
    url = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
    headers = {"Authorization": f"Bearer {api_key}"}
    data = {
        "model": "glm-4-flash",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature
    }
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(url, headers=headers, json=data, timeout=45)
            resp.raise_for_status()
            return resp.json()['choices'][0]['message']['content'].strip()
        except requests.exceptions.Timeout:
            time.sleep(2 ** attempt)
        except Exception as e:
            if attempt == MAX_RETRIES:
                return f"__FAILED__:{e}"
            time.sleep(1)
    return "__FAILED__"

# ══════════════════════════════════════════════════════════════════
#  STEP 0 · 品类自动识别 + 动态维度生成
# ══════════════════════════════════════════════════════════════════

# 通用兜底维度（万能，不含任何品类专属词）
FALLBACK_ASPECTS = {
    "产品质量":   ["质量", "做工", "材质", "耐用", "结实", "工艺"],
    "物流配送":   ["物流", "快递", "发货", "配送", "包装", "破损"],
    "价格性价比": ["价格", "性价比", "划算", "贵", "便宜", "值得"],
    "客服体验":   ["客服", "售后", "态度", "服务", "回复", "退款"],
    "产品功能":   ["效果", "功能", "性能", "好用", "实用", "使用"],
    "外观设计":   ["外观", "颜值", "设计", "好看", "颜色", "款式"],
}

def ai_detect_category_and_aspects(sample_reviews: list) -> tuple[str, dict]:
    """
    ✅ v4.1 升级：同时返回 (category_name, aspects_dict)
    让主流程能正确拿到 AI 识别的品类名
    """
    sample_text = "\n".join([f"- {r}" for r in sample_reviews[:15]])

    prompt = f"""你是一位资深电商数据分析师。
请根据以下买家评论，完成两件事：

第一步：判断这是什么类型的商品（比如：耳机、笔记本电脑、衣服、食品、护肤品、家电等）

第二步：针对这类商品，列出买家最关心的6-8个核心评价维度，以及每个维度对应的关键词。

要求：
1. 维度必须是这类商品特有的，不要用"产品质量"这种万能维度
2. 每个维度给5-8个中文关键词
3. 只返回JSON格式，不要任何解释
4. 格式如下：
{{
  "category": "商品类别名称",
  "aspects": {{
    "维度名称（带emoji）": ["关键词1", "关键词2", "关键词3"],
    "维度名称（带emoji）": ["关键词1", "关键词2", "关键词3"]
  }}
}}

买家评论样本：
{sample_text}"""

    result = call_glm(prompt, temperature=0.3)

    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            parsed   = json.loads(match.group())
            category = parsed.get("category", "").strip()
            aspects  = parsed.get("aspects", {})
            if aspects and category:
                print(f"    🤖 AI识别品类：【{category}】")
                print(f"    📐 动态生成维度：{list(aspects.keys())}")
                return category, aspects   # ✅ 返回品类名 + 维度
    except Exception as e:
        print(f"    ⚠️  AI维度解析失败: {e}，使用通用兜底维度")

    return "", FALLBACK_ASPECTS   # 兜底

# ══════════════════════════════════════════════════════════════════
#  STEP 1 · 硬过滤（纯通用规则，无品类词）
# ══════════════════════════════════════════════════════════════════

DEFAULT_REVIEW_BLACKLIST = {
    "系统默认好评", "此用户没有填写评价。", "此用户没有填写评价",
    "好", "还行", "不错", "挺好的", "可以", "可以的", "还不错",
    "收到了", "已收到", "正在使用中", "暂无评价", "默认好评"
}

SUSPICIOUS_PATTERNS = [
    r"^系统.*好评$",
    r"^默认.*好评$",
    r"没有填写",
    r"^好+$",
    r"^棒+$",
    r"^赞+$",
    r"^[👍🌟⭐✨]+$",
]

def hard_filter(row) -> str:
    content = str(row[COL_CONTENT]).strip()

    if content in DEFAULT_REVIEW_BLACKLIST:
        return "硬过滤_默认文案"

    for pat in SUSPICIOUS_PATTERNS:
        if re.search(pat, content):
            return "硬过滤_默认文案"

    clean_len = len(re.sub(r'[^\u4e00-\u9fffA-Za-z0-9]', '', content))
    if clean_len <= 4:
        return "硬过滤_内容过短"

    try:
        if "00:00:00" in str(row.get(COL_TIME, "")):
            return "硬过滤_凌晨整点"
    except Exception:
        pass

    if len(content) > 5 and len(set(content)) < len(content) * 0.35:
        return "硬过滤_重复内容"

    return "通过"

# ══════════════════════════════════════════════════════════════════
#  STEP 2 · AI软分类
# ══════════════════════════════════════════════════════════════════

def ai_classify(content: str, rating: int, likes: int, likes_threshold: int = 0) -> str:

    likes_line = f"点赞数：{likes}\n" if likes > likes_threshold else ""

    prompt = f"""你是电商平台的评论审核专家。请判断以下评论属于哪个类别。

评论内容："{content}"
买家评分：{rating}星（满分5星）
点赞数：{likes}

分类标准：
- 有效好评：真实买家写的正面评价，有具体使用感受
- 有效差评：真实买家写的负面评价，有具体问题描述
- 商家刷评：内容空洞、疑似刷单、与产品实际使用无关
- 恶意差评：无实际依据的恶意攻击
- 无效评论：内容无意义、无法判断真实性

只返回类别名称，不要任何解释。"""

    result = call_glm(prompt, temperature=0.1)
    valid = {"有效好评", "有效差评", "商家刷评", "恶意差评", "无效评论"}
    return result if result in valid else "无效评论"

# ══════════════════════════════════════════════════════════════════
#  STEP 3 · 维度统计
# ══════════════════════════════════════════════════════════════════

def aspect_analysis(valid_df: pd.DataFrame, aspects: dict) -> dict:
    stats = {}
    for asp_name, keywords in aspects.items():
        pattern = "|".join(re.escape(k) for k in keywords)
        cnt = int(valid_df[COL_CONTENT].str.contains(pattern, na=False).sum())
        stats[asp_name] = cnt
    return stats

# ══════════════════════════════════════════════════════════════════
#  STEP 4 · AI关键词提取
#  ✅ v4.1：prompt 完全通用，示例不再写死品类
# ══════════════════════════════════════════════════════════════════

def ai_extract_keywords(reviews_text: str, sentiment: str, category: str = "商品") -> dict:
    """
    从评论文本中提取关键词及权重
    sentiment: "好评" 或 "差评"
    category:  AI 识别出的品类名，让提取更精准
    """
    if not reviews_text.strip():
        return {}

    prompt = f"""你是电商数据分析专家，请从以下【{category}】的【{sentiment}】评论中，
提取最能代表买家真实感受的关键词。

评论内容：
{reviews_text[:3000]}

要求：
1. 提取15-25个最有代表性的词语或短语（2-6字为佳）
2. 关键词必须和【{category}】这类商品直接相关，不要提取"发货快"、"客服好"等通用词
3. 每个词给一个权重分数（1-100，越重要越高）
4. 只返回JSON格式，不要任何解释
5. 格式：{{"词语": 权重, "词语": 权重}}"""

    result = call_glm(prompt, temperature=0.2)

    if "__NO_KEY__" in result or "__FAILED__" in result:
        return {}

    try:
        match = re.search(r'\{.*\}', result, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            return {k: int(v) for k, v in parsed.items() if isinstance(v, (int, float))}
    except Exception as e:
        print(f"    ⚠️  关键词解析失败: {e}")

    return {}

# ══════════════════════════════════════════════════════════════════
#  STEP 5 · AI建议生成
# ══════════════════════════════════════════════════════════════════

def ai_generate_suggestion(
    product_name: str,
    category_name: str,
    good_kw: dict,
    bad_kw: dict,
    aspect_stats: dict,
    total: int,
    good_count: int,
    bad_count: int,
    fake_count: int
) -> str:

    good_rate = round(good_count / total * 100, 1) if total > 0 else 0
    fake_rate = round(fake_count / total * 100, 1) if total > 0 else 0

    top_aspects = sorted(aspect_stats.items(), key=lambda x: -x[1])[:5]
    top_str  = "、".join([f"{k}({v}条)" for k, v in top_aspects if v > 0]) or "暂无数据"
    good_str = "、".join(list(good_kw.keys())[:8]) if good_kw else "暂无"
    bad_str  = "、".join(list(bad_kw.keys())[:8])  if bad_kw  else "暂无"

    prompt = f"""你是一位经验丰富的电商运营顾问，正在给一位小商家写产品分析报告。
请用亲切、通俗易懂的语言，让商家一看就明白，一学就能用。

商品名称：{product_name}
商品类别：{category_name}
总评论数：{total}条
有效好评率：{good_rate}%
识别刷评/无效：{fake_count}条（占{fake_rate}%）
买家最关注：{top_str}
买家夸的：{good_str}
买家吐槽的：{bad_str}

请严格按照下面的格式输出，不要添加任何多余内容：

亲爱的商家，您好！

这是您的产品【{product_name}】的用户真实反馈分析报告，希望能帮助您更好地了解买家心声，持续提升产品竞争力。

**一、买家最认可您产品的哪些地方？**
[用2-3句话，具体说出买家最满意的核心优点，突出产品真实价值]

**二、买家反映最集中的问题是什么？**
[直接点明1-2个最核心的痛点，客观描述，不要模糊带过]

**三、建议您马上着手改进的三件事**
1. 【最优先】
2. 【较重要】
3. 【加分项】

**四、下一步选品与经营方向建议**
1. 
2.

感谢您对【言之有品】的信任和使用，祝您工作顺利，万事顺遂！"""

    result = call_glm(prompt, temperature=0.6)

    if "__NO_KEY__" in result or "__FAILED__" in result:
        return ("亲爱的商家，您好！\n\n"
                "AI分析功能暂时不可用，请检查API Key配置。\n\n"
                "感谢您对【言之有品】的信任和使用，祝您工作顺利，万事顺遂！")
    return result

# ══════════════════════════════════════════════════════════════════
#  词云生成
#  ✅ v4.1：STOP_WORDS 移除所有品类专属词，只保留通用虚词
# ══════════════════════════════════════════════════════════════════

def find_font() -> str:
    # ✅ 优先使用项目根目录的字体（Render 服务器专用）
    local_font = "NotoSansSC-VariableFont_wght.ttf"
    if os.path.exists(local_font):
        return local_font

    candidates = [
        "/mnt/c/Windows/Fonts/simsun.ttc",
        "/mnt/c/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ]
    for p in candidates:
        if os.path.exists(p):
            return p
    for f in fm.findSystemFonts():
        if any(k in f.lower() for k in ['simhei','simsun','noto','wqy','cjk','heiti','yahei']):
            return f
    return ""

FONT_PATH = find_font()

# ✅ 只保留真正通用的虚词/无意义词，不含任何品类专属词
STOP_WORDS = {
    # 通用虚词
    "感觉", "觉得", "感受", "但是", "没有", "几乎",
    "真的", "确实", "还是", "比较", "已经", "然后",
    "这个", "那个", "超级", "非常", "特别",
    "很好", "不错", "还好", "挺好", "一般", "不少",
    # 电商通用词（对分析无价值）
    "东西", "产品", "购买", "入手", "使用",
    "点赞", "回复", "收到", "发货",
    "快递", "客服", "服务", "服务态度",
}

def green_color_func(word, font_size, position, orientation, random_state=None, **kwargs):
    colors = ["#C6E2D5", "#84c3b7", "#62B077", "#4a9e6b", "#018B33", "#88d8db"]
    ratio = min(font_size / 80, 1.0)
    idx = int(ratio * (len(colors) - 1))
    return colors[idx]

def orange_color_func(word, font_size, position, orientation, random_state=None, **kwargs):
    colors = ["#FFFCC1", "#FEDB75", "#f2b56f", "#FEBC07", "#f57c6e", "#e8622a"]
    ratio = min(font_size / 80, 1.0)
    idx = int(ratio * (len(colors) - 1))
    return colors[idx]

def generate_wordcloud(freq_dict: dict, save_path: str, colormap: str = "Blues"):
    if not freq_dict:
        return

    filtered = {
        w: s for w, s in freq_dict.items()
        if w not in STOP_WORDS and len(w) >= 2
    }
    if not filtered:
        print(f"    ⚠️  过滤后词频为空，跳过")
        return

    color_func = green_color_func if colormap == "Blues" else orange_color_func

    mask = None
    if os.path.exists(MASK_PATH):
        try:
            mask_img = Image.open(MASK_PATH).convert("RGBA")
            mask_img = mask_img.resize((800, 500), Image.LANCZOS)
            bg = Image.new("RGBA", mask_img.size, (255, 255, 255, 255))
            bg.paste(mask_img, mask=mask_img.split()[3])
            gray = np.mean(np.array(bg.convert("RGB")), axis=2)
            mask = np.where(gray < 128, 0, 255).astype(np.uint8)
        except Exception as e:
            print(f"    ⚠️  蒙版加载失败: {e}，使用矩形")

    try:
        wc = WordCloud(
            font_path=FONT_PATH or None,
            mask=mask,
            background_color='white',
            color_func=color_func,
            max_words=80,
            max_font_size=90,
            min_font_size=10,
            prefer_horizontal=0.7,
            relative_scaling=0.6,
            collocations=False,
            width=800,
            height=500,
            margin=4,
        ).generate_from_frequencies(filtered)
        wc.to_file(save_path)
        print(f"    ✅ 词云: {os.path.basename(save_path)}")
    except Exception as e:
        print(f"    ❌ 词云失败: {e}")

# ══════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════

def run():
    print("═" * 60)
    print("  言之有'品' · 通用品类自适应分析 v4.1  🏆")
    print("  支持任意商品品类，零硬编码，维度全自动识别")
    print("═" * 60)

    try:
        df = pd.read_csv(INPUT_CSV)
        print(f"\n✅ 读取成功：{len(df)} 条评论，{df[COL_PRODUCT].nunique()} 个产品")
        print(f"   产品列表：{df[COL_PRODUCT].unique().tolist()}")
    except FileNotFoundError:
        print(f"❌ 找不到: {INPUT_CSV}")
        return
    
    df[COL_LIKES] = pd.to_numeric(df[COL_LIKES], errors='coerce').fillna(0).astype(int)
    non_zero_likes = df[df[COL_LIKES] > 0][COL_LIKES]
    if len(non_zero_likes) == 0:
        likes_threshold = 0
        print("  · 点赞数全为0，该信号将自动忽略，不影响分析质量")
    else:
        likes_threshold = int(non_zero_likes.quantile(0.75))
        if likes_threshold < 5:
            likes_threshold = 0
            print(f"  · 点赞数普遍偏低（75分位={likes_threshold}），该信号将自动忽略，不影响分析质量")
        else:
            print(f"  · 点赞数有效，动态阈值={likes_threshold}，高于此值才作为参考信号")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    all_results = []

    for product_name, pdf in df.groupby(COL_PRODUCT):
        safe_name = re.sub(r'[\\/*?:"<>|]', '_', str(product_name)).strip()
        folder = os.path.join(OUTPUT_DIR, safe_name)
        os.makedirs(folder, exist_ok=True)

        print(f"\n{'─'*55}")
        print(f"📦 分析产品：【{product_name}】（共{len(pdf)}条）")

        work_df = pdf.copy().reset_index(drop=True)

        # ── A: 硬过滤 ───────────────────────────────────────────
        print("  [1/6] 硬过滤...")
        work_df['hard_label'] = work_df.apply(hard_filter, axis=1)
        hard_out = work_df[work_df['hard_label'] != '通过']
        hard_in  = work_df[work_df['hard_label'] == '通过'].copy()
        print(f"    剔除: {len(hard_out)} 条 | 保留: {len(hard_in)} 条")
        for label, cnt in hard_out['hard_label'].value_counts().items():
            print(f"      · {label}: {cnt}条")

        # ── B: 品类识别 + 动态维度生成 ─────────────────────────
        print("  [2/6] 品类识别 + 动态维度生成...")
        sample_reviews  = hard_in[COL_CONTENT].tolist()[:15]
        # ✅ v4.1：正确接收 AI 返回的品类名
        ai_category_name, dynamic_aspects = ai_detect_category_and_aspects(sample_reviews)
        # AI识别到品类就用AI的，否则用商品名兜底
        category_name = ai_category_name if ai_category_name else product_name

        # ── C: AI软分类 ─────────────────────────────────────────
        print(f"  [3/6] AI软分类（并发{MAX_WORKERS}线程）...")
        if not os.getenv("ZHIPUAI_API_KEY"):
            hard_in['ai_category'] = "AI未开启"
        else:
            start = time.time()
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
                futures = {
                    executor.submit(
                        ai_classify,
                        row[COL_CONTENT],
                        int(row.get(COL_RATING, 3)),
                        int(row.get(COL_LIKES, 0))
                    ): idx
                    for idx, row in hard_in.iterrows()
                }
                results = {}
                for i, future in enumerate(as_completed(futures), 1):
                    results[futures[future]] = future.result()
                    print(f"    进度: {i}/{len(futures)}", end='\r')

            hard_in['ai_category'] = hard_in.index.map(results)
            print(f"\n    ✅ 耗时 {time.time()-start:.1f}s")

        work_df['ai_category'] = work_df['hard_label']
        work_df.loc[hard_in.index, 'ai_category'] = hard_in['ai_category']

        cat_counts = work_df['ai_category'].value_counts().to_dict()
        good_df    = work_df[work_df['ai_category'] == '有效好评']
        bad_df     = work_df[work_df['ai_category'] == '有效差评']
        fake_count = sum(v for k, v in cat_counts.items()
                         if k not in ('有效好评', '有效差评'))
        print(f"    {cat_counts}")

        # ── D: 维度分析 ─────────────────────────────────────────
        print("  [4/6] 维度分析（动态维度）...")
        valid_df     = work_df[work_df['ai_category'].isin(['有效好评', '有效差评'])]
        aspect_stats = aspect_analysis(valid_df, dynamic_aspects)
        for asp, cnt in sorted(aspect_stats.items(), key=lambda x: -x[1]):
            if cnt > 0:
                print(f"    {asp}: {cnt}条")

        # ── E: AI关键词 + 词云 ───────────────────────────────────
        
        # ── E: AI关键词 + 词云 ───────────────────────────────────
        print("  [5/6] 关键词提取 + 词云...")
        good_kw = ai_extract_keywords(
            " ".join(good_df[COL_CONTENT].tolist()), "好评", category_name)
        bad_kw  = ai_extract_keywords(
            " ".join(bad_df[COL_CONTENT].tolist()),  "差评", category_name)

        # ✅ 词云传dict
        generate_wordcloud(good_kw, os.path.join(folder, "词云_真实优点.png"), "Blues")
        generate_wordcloud(bad_kw,  os.path.join(folder, "词云_真实缺点.png"), "YlOrRd")

        # ✅ 新增：转成字符串供JSON和扣子使用
        good_kw_str = "、".join(good_kw.keys()) if good_kw else ""
        bad_kw_str  = "、".join(bad_kw.keys())  if bad_kw  else ""


        # ✅ 词云传dict
        generate_wordcloud(good_kw, os.path.join(folder, "词云_真实优点.png"), "Blues")
        generate_wordcloud(bad_kw,  os.path.join(folder, "词云_真实缺点.png"), "YlOrRd")

        # ✅ 新增：转成字符串供JSON和扣子使用
        good_kw_str = "、".join(good_kw.keys()) if good_kw else ""
        bad_kw_str  = "、".join(bad_kw.keys())  if bad_kw  else ""
        
        # ── F: AI建议 ────────────────────────────────────────────
        print("  [6/6] AI建议生成...")
        suggestion = ai_generate_suggestion(
            product_name, category_name,
            good_kw, bad_kw, aspect_stats,
            total=len(work_df),
            good_count=len(good_df),
            bad_count=len(bad_df),
            fake_count=fake_count
        )

        # ── 保存 ─────────────────────────────────────────────────
        work_df.to_csv(
            os.path.join(folder, "所有评论_含分析标签.csv"),
            index=False, encoding='utf-8-sig'
        )
        with open(os.path.join(folder, "给您的报告.txt"), 'w', encoding='utf-8') as f:
            f.write(suggestion)

        product_result = {
            "product_name":          product_name,
            "category_name":         category_name,       # ✅ 现在是AI识别的真实品类
            "dynamic_aspects":       dynamic_aspects,
            "total_reviews":         len(work_df),
            "category_distribution": cat_counts,
            "aspect_mention_count":  aspect_stats,
            'good_keywords': good_kw_str,
            'bad_keywords':  bad_kw_str,
            "suggestion":            suggestion,
        }
        with open(os.path.join(folder, "结构化分析结果.json"), 'w', encoding='utf-8') as f:
            json.dump(product_result, f, ensure_ascii=False, indent=2)

        all_results.append(product_result)
        print(f"  ✅ 【{product_name}】完成，品类：【{category_name}】")

    # 合并所有产品JSON → 网页读这个
    with open(MERGED_JSON_PATH, 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"\n{'═'*60}")
    print(f"🎉 全部完成！{len(all_results)} 个产品")
    print(f"   网页读取：{MERGED_JSON_PATH}")
    print(f"   分析报告：{OUTPUT_DIR}/")
    print(f"{'═'*60}")

if __name__ == "__main__":
    if not FONT_PATH:
        print("⚠️  未找到中文字体")
    if not os.getenv("ZHIPUAI_API_KEY"):
        print("⚠️  未检测到 API Key")
    run()
