from DrissionPage import Chromium, ChromiumOptions

# 创建配置，指定端口（避免和普通Chrome冲突）
co = ChromiumOptions()
co.set_local_port(9333)  # 换一个不常用的端口

tab = Chromium(co).latest_tab
tab.get('http://DrissionPage.cn')