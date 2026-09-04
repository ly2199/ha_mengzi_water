"""Constants for the Mengzi Water (蒙自城镇供水) integration."""
from __future__ import annotations

DOMAIN = "mengzi_water"

# 配置项
CONF_COOKIE = "session_cookie"       # 网上营业厅会话 Cookie(Ares.Core.Session.JXSWPHALL=...)
CONF_SCAN_INTERVAL = "scan_interval" # 轮询间隔(秒)
CONF_NAME = "name"                   # 可选名称

# 默认值
DEFAULT_SCAN_INTERVAL = 1800         # 默认 30 分钟轮询一次
MIN_SCAN_INTERVAL = 300              # 最短 5 分钟

# 服务端
API_BASE_URL = "https://swp.mzczgs.com/Hall/"
COOKIE_NAME = "Ares.Core.Session.JXSWPHALL"
COMPANY_NAME = "蒙自市城镇供水有限责任公司"
