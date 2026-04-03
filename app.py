from flask import Flask, request, jsonify
import json
import subprocess
import sys
import os
import tempfile
import requests
import time
from urllib.parse import urlencode
import shutil

app = Flask(__name__)

# ============ 配置 ============
RENDER_ANALYZE_API = "https://yanzhi-youpin.onrender.com/analyze"
CHROME_PATH = r'C:\Program Files\Google\Chrome\Application\chrome.exe'  # Windows 路径

@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({"status": "ok", "message": "Backend is running"}), 200

@app.route('/scrape-and-analyze', methods=['POST'])
def scrape_and_analyze():
    """
    完整流程：JD 链接 → 爬虫 → 分析 → 返回结果
    
    输入：
    {
        "jd_url": "https://item.jd.com/10127955410850.html",
        "product_name": "商品名称（可选）"
    }
    """
    
    try:
        data = request.get_json()
        jd_url = data.get('jd_url')
        product_name = data.get('product_name', '未知商品')
        
        if not jd_url:
            return jsonify({
                "success": False, 
                "message": "缺少必需参数 jd_url"
            }), 400
        
        print(f"\n{'='*60}")
        print(f"【新的分析请求】")
        print(f"JD 链接: {jd_url}")
        print(f"{'='*60}")
        
        # ========== 步骤 1：准备爬虫 ==========
        print("\n📍 步骤 1/5：准备爬虫脚本...")
        
        try:
            # 读取原始爬虫代码
            with open('c.py', 'r', encoding='utf-8') as f:
                spider_code = f.read()
            
            # 替换商品链接
            original_url = "https://item.jd.com/10127955410850.html"
            if original_url in spider_code:
                spider_code = spider_code.replace(original_url, jd_url)
            else:
                # 如果找不到原始链接，尝试替换任何 JD 链接
                import re
                spider_code = re.sub(
                    r'dp\.get\([\'"]https://item\.jd\.com/\d+\.html[\'"]',
                    f"dp.get('{jd_url}'",
                    spider_code
                )
            
            # 写到临时文件
            temp_dir = tempfile.gettempdir()
            temp_spider = os.path.join(temp_dir, 'temp_spider_' + str(time.time()).replace('.', '_') + '.py')
            with open(temp_spider, 'w', encoding='utf-8') as f:
                f.write(spider_code)
            
            print(f"✅ 爬虫脚本已准备到临时文件: {temp_spider}")
            
        except Exception as e:
            print(f"❌ 准备爬虫失败: {str(e)}")
            return jsonify({
                "success": False,
                "message": f"准备爬虫脚本失败: {str(e)}"
            }), 500
        
        # ========== 步骤 2：运行爬虫 ==========
        print("\n📍 步骤 2/5：启动爬虫（这需要 5-30 分钟）...")
        
        try:
            # 运行爬虫脚本
            result = subprocess.run(
                [sys.executable, temp_spider],
                timeout=1800,  # 30 分钟超时
                capture_output=True,
                text=True,
                cwd=os.getcwd()
            )
            
            # 打印爬虫输出
            if result.stdout:
                print("爬虫日志:")
                print(result.stdout[:500])  # 只打印前 500 字符
            
            if result.returncode != 0:
                error_msg = result.stderr if result.stderr else "未知错误"
                print(f"❌ 爬虫执行失败 (错误代码: {result.returncode})")
                print(f"错误信息: {error_msg[:200]}")
                
                return jsonify({
                    "success": False,
                    "message": f"爬虫执行失败: {error_msg[:100]}"
                }), 500
            
            print("✅ 爬虫执行完成")
            
        except subprocess.TimeoutExpired:
            print("❌ 爬虫执行超时（>30分钟）")
            return jsonify({
                "success": False,
                "message": "爬虫执行超时，可能是网络问题或页面加载慢"
            }), 408
        except Exception as e:
            print(f"❌ 爬虫执行出错: {str(e)}")
            return jsonify({
                "success": False,
                "message": f"爬虫执行异常: {str(e)}"
            }), 500
        finally:
            # 清理临时文件
            if os.path.exists(temp_spider):
                try:
                    os.remove(temp_spider)
                except:
                    pass
        
        # ========== 步骤 3：读取评论数据 ==========
        print("\n📍 步骤 3/5：读取爬虫输出数据...")
        
        try:
            reviews_file = '评论.json'
            
            if not os.path.exists(reviews_file):
                print(f"❌ 找不到 {reviews_file}，爬虫可能没有生成")
                return jsonify({
                    "success": False,
                    "message": "爬虫未生成评论文件，可能是网络问题或商品不存在"
                }), 500
            
            with open(reviews_file, 'r', encoding='utf-8') as f:
                reviews_data = json.load(f)
            
            review_count = len(reviews_data.get('reviews', []))
            print(f"✅ 成功读取 {review_count} 条评论")
            
            if review_count == 0:
                print("⚠️ 警告：没有爬取到任何评论")
                return jsonify({
                    "success": False,
                    "message": "未爬取到评论数据，请检查商品链接是否有效"
                }), 400
            
        except json.JSONDecodeError as e:
            print(f"❌ 评论文件 JSON 格式错误: {str(e)}")
            return jsonify({
                "success": False,
                "message": "爬虫输出格式错误"
            }), 500
        except Exception as e:
            print(f"❌ 读取数据失败: {str(e)}")
            return jsonify({
                "success": False,
                "message": f"读取评论数据失败: {str(e)}"
            }), 500
        
        # ========== 步骤 4：调用分析 API ==========
        print("\n📍 步骤 4/5：调用分析引擎...")
        
        try:
            start_time = time.time()
            
            response = requests.post(
                RENDER_ANALYZE_API,
                json=reviews_data,
                timeout=180,
                headers={"Content-Type": "application/json"}
            )
            
            elapsed_time = time.time() - start_time
            print(f"✅ 收到分析结果 (耗时 {elapsed_time:.1f} 秒)")
            
            response.raise_for_status()
            result = response.json()
            
            if not result.get('success'):
                print(f"⚠️ 分析返回错误: {result.get('message')}")
            
        except requests.exceptions.Timeout:
            print("❌ 分析引擎响应超时（>180秒）")
            return jsonify({
                "success": False,
                "message": "分析引擎响应超时"
            }), 408
        except requests.exceptions.ConnectionError:
            print("❌ 无法连接分析 API")
            return jsonify({
                "success": False,
                "message": f"无法连接分析 API: {RENDER_ANALYZE_API}"
            }), 503
        except Exception as e:
            print(f"❌ 调用分析 API 失败: {str(e)}")
            return jsonify({
                "success": False,
                "message": f"分析失败: {str(e)}"
            }), 500
        
        # ========== 步骤 5：返回最终结果 ==========
        print("\n📍 步骤 5/5：打包返回结果...")
        
        final_result = {
            "success": True,
            "message": "分析成功",
            "data": {
                "jd_url": jd_url,
                "product_name": product_name,
                "review_count": review_count,
                "analysis_result": result
            }
        }
        
        print(f"\n{'='*60}")
        print(f"✨ 处理完成！返回结果给扣子")
        print(f"{'='*60}\n")
        
        return jsonify(final_result), 200
        
    except Exception as e:
        print(f"\n❌ 发生未预期的错误: {str(e)}")
        return jsonify({
            "success": False,
            "message": f"服务器错误: {str(e)}"
        }), 500

@app.route('/direct-analyze', methods=['POST'])
def direct_analyze():
    """
    直接调用分析 API（如果已经有评论数据）
    """
    try:
        reviews_data = request.get_json()
        
        if not reviews_data or 'reviews' not in reviews_data:
            return jsonify({
                "success": False,
                "message": "缺少 reviews 参数"
            }), 400
        
        response = requests.post(
            RENDER_ANALYZE_API,
            json=reviews_data,
            timeout=180
        )
        response.raise_for_status()
        result = response.json()
        
        return jsonify(result), 200
        
    except Exception as e:
        return jsonify({
            "success": False,
            "message": f"错误: {str(e)}"
        }), 500

if __name__ == '__main__':
    import os
    
    # 从环境变量获取端口，默认 10000（Render 要求）
    port = int(os.environ.get('PORT', 10000))
    
    print("🚀 启动言之有品后端服务...")
    if port == 10000:
        print("运行在 Render 上")
        print("健康检查: https://yzp0.onrender.com/health")
    else:
        print(f"本地访问: http://localhost:{port}")
        print(f"健康检查: http://localhost:{port}/health")
    
    app.run(debug=False, host='0.0.0.0', port=port)
