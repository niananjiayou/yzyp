from DrissionPage import ChromiumOptions

path = r'C:\Users\Lenovo\AppData\Local\Google\Chrome\Application\chrome.exe'  # 请改为你电脑内Chrome可执行文件路径
ChromiumOptions().set_browser_path(path).save()
