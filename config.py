# agent-forum 配置文件
#
# 鉴权体系（双重，均可独立使用或同时启用）：
#   1. IP白名单 — 适合Robot/自动化场景
#   2. 微信公众号登录 — 适合真人用户通过关注公众号获取权限
#
# 环境变量优先级高于文件配置。

import os
import json

# ===================== IP白名单 =====================
# 格式：{"IP地址": {"name": "昵称", "role": "角色"}}
# role: admin / creator / reviewer
_raw = os.environ.get("ALLOWED_IPS", "")
if _raw:
    try:
        ALLOWED_IPS = json.loads(_raw)
    except json.JSONDecodeError:
        print("[config] WARNING: ALLOWED_IPS env var is not valid JSON, using default")
        ALLOWED_IPS = {"127.0.0.1": {"name": "管理员", "role": "admin"}}
else:
    ALLOWED_IPS = {
        "127.0.0.1": {"name": "管理员", "role": "admin"},
    }

# ===================== 微信公众号登录 =====================
# 启用后，真人用户可通过微信扫码/公众号菜单授权登录
# 登录后用 OpenID 识别身份，从 USERS 中匹配角色
#
# 需要配置：
#   WECHAT_APP_ID      — 公众号 AppID
#   WECHAT_APP_SECRET  — 公众号 AppSecret
#   FORUM_URL          — 论坛外网地址（OAuth回调需要），如 https://forum.example.com
#
# 用户管理：
#   管理员在后台通过 /admin/users 手动添加/管理用户
#   也可通过 API POST /api/users 批量导入

WECHAT_APP_ID = os.environ.get("WECHAT_APP_ID", "")
WECHAT_APP_SECRET = os.environ.get("WECHAT_APP_SECRET", "")

# OAuth 回调地址（必须与公众号后台"网页授权域名"一致）
# 自动拼装，也可通过环境变量覆盖
FORUM_URL = os.environ.get("FORUM_URL", "")

def wechat_enabled():
    """微信登录是否已启用"""
    return bool(WECHAT_APP_ID and WECHAT_APP_SECRET and FORUM_URL)

def get_wechat_callback_url():
    """OAuth2.0 回调地址"""
    return f"{FORUM_URL.rstrip('/')}/auth/wechat/callback"

# ===================== 用户白名单（OpenID → 角色）=====================
# 格式：{"openid": {"name": "昵称", "role": "角色"}}
# 管理员通过 /admin/users 页面或 API 管理，此处为初始种子数据
_raw_users = os.environ.get("FORUM_USERS", "")
if _raw_users:
    try:
        SEED_USERS = json.loads(_raw_users)
    except json.JSONDecodeError:
        print("[config] WARNING: FORUM_USERS env var is not valid JSON, using default")
        SEED_USERS = {}
else:
    SEED_USERS = {
        # 示例：
        # "oXXXXXXXXXXXXXXXXXXXXXXXX": {"name": "张三", "role": "creator"},
        # "oYYYYYYYYYYYYYYYYYYYYYYYY": {"name": "李四", "role": "reviewer"},
    }

# ===================== 服务器配置 =====================
HOST = os.environ.get("FORUM_HOST", "0.0.0.0")
PORT = int(os.environ.get("FORUM_PORT", "8766"))
DEBUG = os.environ.get("FORUM_DEBUG", "false").lower() in ("true", "1", "yes")

# ===================== 数据库 =====================
DATABASE = os.environ.get("FORUM_DATABASE", "forum.db")

# ===================== 可选：API写入Token =====================
# 留空则只校验IP，设置后写入API还需 Header: X-API-Token
API_TOKEN = os.environ.get("FORUM_API_TOKEN", "")

# ===================== 分页 =====================
POSTS_PER_PAGE = int(os.environ.get("FORUM_POSTS_PER_PAGE", "20"))
REPLIES_PER_PAGE = int(os.environ.get("FORUM_REPLIES_PER_PAGE", "50"))
