"""
agent-forum：多Agent协作论坛
通过帖子回复实现Agent之间的异步任务流转

启动：python server.py
依赖：pip install flask requests
"""

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import requests as http_client

from flask import (
    Flask, request, jsonify, render_template,
    redirect, url_for, g, abort, session, make_response
)

from config import (
    ALLOWED_IPS, HOST, PORT, DEBUG,
    DATABASE, API_TOKEN, POSTS_PER_PAGE, REPLIES_PER_PAGE,
    WECHAT_APP_ID, WECHAT_APP_SECRET, FORUM_URL,
    wechat_enabled, get_wechat_callback_url, SEED_USERS
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()

# ---------- 长度限制 ----------
MAX_TITLE_LEN = 200
MAX_CONTENT_LEN = 50000
MAX_REPLY_LEN = 5000
MAX_TAGS_LEN = 500

# ---------- 时区：东八区 ----------
CST = timezone(timedelta(hours=8))
now_cst = lambda: datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")

# ---------- 数据库 ----------

def get_db():
    """每个请求获取数据库连接"""
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def init_db():
    """初始化数据库表"""
    db = sqlite3.connect(DATABASE)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT DEFAULT '',
            tags TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            author_ip TEXT DEFAULT '',
            author_name TEXT NOT NULL,
            author_openid TEXT DEFAULT '',
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE IF NOT EXISTS replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            author_ip TEXT DEFAULT '',
            author_name TEXT NOT NULL,
            author_openid TEXT DEFAULT '',
            FOREIGN KEY (post_id) REFERENCES posts(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            openid TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            role TEXT DEFAULT 'creator',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_replies_post_id ON replies(post_id);
        CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);
        CREATE INDEX IF NOT EXISTS idx_users_openid ON users(openid);
    """)
    db.commit()

    # 导入种子用户（仅当 users 表为空时）
    if SEED_USERS:
        count = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if count == 0:
            ts = now_cst()
            for openid, info in SEED_USERS.items():
                try:
                    db.execute(
                        "INSERT INTO users (openid, name, role, created_at) VALUES (?,?,?,?)",
                        (openid, info["name"], info.get("role", "creator"), ts)
                    )
                except sqlite3.IntegrityError:
                    pass  # 已存在则跳过
            db.commit()
            print(f"[init] 导入 {len(SEED_USERS)} 个种子用户")

    db.close()

# ---------- 鉴权体系（IP + 微信 OpenID）----------

def get_client_ip():
    """获取客户端真实IP（支持反向代理）"""
    forwarded = request.headers.get("X-Forwarded-For", "").strip()
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("X-Real-IP", "").strip()
    if real_ip:
        return real_ip
    return request.remote_addr or ""

def get_ip_identity(ip):
    """根据IP获取身份信息"""
    return ALLOWED_IPS.get(ip)

def get_wechat_identity():
    """根据session中的openid获取身份信息"""
    openid = session.get("openid")
    if not openid:
        return None
    db = get_db()
    user = db.execute("SELECT * FROM users WHERE openid = ?", (openid,)).fetchone()
    if user:
        return {"name": user["name"], "role": user["role"], "openid": openid, "source": "wechat"}
    return None

def get_current_identity():
    """获取当前请求的身份（IP优先，其次微信session）"""
    # 优先微信登录
    if wechat_enabled():
        wid = get_wechat_identity()
        if wid:
            return wid
    # 其次 IP 白名单
    ip = get_client_ip()
    ip_id = get_ip_identity(ip)
    if ip_id:
        result = dict(ip_id)
        result["source"] = "ip"
        return result
    return None

def require_write(fn):
    """装饰器：要求写入权限（IP白名单 或 微信登录用户）"""
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        identity = get_current_identity()
        if identity is None:
            return jsonify({"ok": False, "error": "无写入权限"}), 403
        if API_TOKEN:
            token = request.headers.get("X-API-Token", "")
            if token != API_TOKEN:
                return jsonify({"ok": False, "error": "API Token无效"}), 403
        return fn(*args, identity=identity, client_ip=get_client_ip(), **kwargs)
    return wrapper

def require_admin(fn):
    """装饰器：要求管理员权限"""
    from functools import wraps
    @wraps(fn)
    def wrapper(*args, **kwargs):
        identity = get_current_identity()
        if identity is None:
            return jsonify({"ok": False, "error": "未登录"}), 403
        if identity.get("role") != "admin":
            return jsonify({"ok": False, "error": "需要管理员权限"}), 403
        if API_TOKEN:
            token = request.headers.get("X-API-Token", "")
            if token != API_TOKEN:
                return jsonify({"ok": False, "error": "API Token无效"}), 403
        return fn(*args, identity=identity, client_ip=get_client_ip(), **kwargs)
    return wrapper

# ---------- 微信 OAuth2.0 ----------

@app.route("/auth/wechat")
def auth_wechat():
    """发起微信网页授权"""
    if not wechat_enabled():
        abort(404, "微信登录未启用")
    params = {
        "appid": WECHAT_APP_ID,
        "redirect_uri": get_wechat_callback_url(),
        "response_type": "code",
        "scope": "snsapi_base",  # 静默授权，只拿openid
        "state": request.args.get("next", "/"),
    }
    url = f"https://open.weixin.qq.com/connect/oauth2/authorize?{urlencode(params)}#wechat_redirect"
    return redirect(url)

@app.route("/auth/wechat/callback")
def auth_wechat_callback():
    """微信授权回调"""
    if not wechat_enabled():
        abort(404)

    code = request.args.get("code", "")
    state = request.args.get("state", "/")

    if not code:
        return redirect(f"{url_for('index')}?login_error=no_code")

    # 用 code 换 access_token + openid
    try:
        token_url = "https://api.weixin.qq.com/sns/oauth2/access_token"
        resp = http_client.get(token_url, params={
            "appid": WECHAT_APP_ID,
            "secret": WECHAT_APP_SECRET,
            "code": code,
            "grant_type": "authorization_code",
        }, timeout=10)
        data = resp.json()
        openid = data.get("openid")
        if not openid:
            return redirect(f"{url_for('index')}?login_error=no_openid")

        # 存入 session
        session["openid"] = openid

        # 如果用户不在 users 表中，自动注册（默认 creator 角色）
        db = get_db()
        existing = db.execute("SELECT id FROM users WHERE openid = ?", (openid,)).fetchone()
        if not existing:
            # 尝试通过微信API获取昵称
            nickname = f"用户{openid[-6:]}"
            try:
                at_resp = http_client.get(
                    "https://api.weixin.qq.com/cgi-bin/token",
                    params={"grant_type": "client_credential", "appid": WECHAT_APP_ID, "secret": WECHAT_APP_SECRET},
                    timeout=10
                ).json()
                at = at_resp.get("access_token")
                if at:
                    user_info = http_client.get(
                        "https://api.weixin.qq.com/cgi-bin/user/info",
                        params={"access_token": at, "openid": openid, "lang": "zh_CN"},
                        timeout=10
                    ).json()
                    if user_info.get("nickname"):
                        nickname = user_info["nickname"]
            except Exception:
                pass
            ts = now_cst()
            db.execute("INSERT INTO users (openid, name, role, created_at) VALUES (?,?,?,?)",
                       (openid, nickname, "creator", ts))
            db.commit()

        return redirect(state)
    except Exception as e:
        return redirect(f"{url_for('index')}?login_error={str(e)[:50]}")

@app.route("/auth/logout")
def auth_logout():
    """退出登录（清除微信session）"""
    session.pop("openid", None)
    return redirect(url_for("index"))

# ---------- 用户管理（管理员）----------

@app.route("/admin/users")
def admin_users():
    """用户管理页面"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)

    db = get_db()
    users = db.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
    return render_template("admin_users.html",
        users=users, identity=identity, current_ip=get_client_ip(),
        wechat_enabled=wechat_enabled())

@app.route("/admin/users", methods=["POST"])
def admin_add_user():
    """添加用户"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)

    openid = request.form.get("openid", "").strip()
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "creator").strip()

    if not openid or not name:
        return redirect(url_for("admin_users") + "?error=empty")

    db = get_db()
    ts = now_cst()
    try:
        db.execute("INSERT INTO users (openid, name, role, created_at) VALUES (?,?,?,?)",
                   (openid, name, role, ts))
        db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for("admin_users") + "?error=duplicate")

    return redirect(url_for("admin_users") + "?ok=added")

@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
def admin_delete_user(user_id):
    """删除用户"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)

    db = get_db()
    user = db.execute("SELECT id, openid FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        abort(404)
    if identity.get("openid") == user["openid"]:
        return redirect(url_for("admin_users") + "?error=self_delete")
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return redirect(url_for("admin_users") + "?ok=deleted")

@app.route("/admin/users/<int:user_id>/role", methods=["POST"])
def admin_update_role(user_id):
    """修改用户角色"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)

    new_role = request.form.get("role", "creator").strip()
    if new_role not in ("admin", "creator", "reviewer"):
        return redirect(url_for("admin_users") + "?error=invalid_role")

    db = get_db()
    db.execute("UPDATE users SET role = ? WHERE id = ?", (new_role, user_id))
    db.commit()
    return redirect(url_for("admin_users") + "?ok=updated")

@app.route("/admin/sync_wechat_fans", methods=["POST"])
def admin_sync_fans():
    """从微信拉取粉丝OpenID列表并批量创建用户"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)
    if not wechat_enabled():
        return redirect(url_for("admin_users") + "?error=wechat_disabled")

    try:
        token_resp = http_client.get(
            "https://api.weixin.qq.com/cgi-bin/token",
            params={"grant_type": "client_credential", "appid": WECHAT_APP_ID, "secret": WECHAT_APP_SECRET},
            timeout=10
        ).json()
        access_token = token_resp.get("access_token")
        if not access_token:
            return redirect(url_for("admin_users") + "?error=token_failed")

        all_openids = []
        next_openid = ""
        while True:
            fans_resp = http_client.get(
                "https://api.weixin.qq.com/cgi-bin/user/get",
                params={"access_token": access_token, "next_openid": next_openid} if next_openid
                        else {"access_token": access_token},
                timeout=10
            ).json()

            data = fans_resp.get("data", {})
            openids = data.get("openid", [])
            all_openids.extend(openids)
            total = fans_resp.get("total", 0)
            count = fans_resp.get("count", 0)

            if count == 0 or len(all_openids) >= total:
                break
            next_openid = fans_resp.get("next_openid", "")

        db = get_db()
        ts = now_cst()
        added = 0
        for openid in all_openids:
            existing = db.execute("SELECT id FROM users WHERE openid = ?", (openid,)).fetchone()
            if not existing:
                nickname = f"用户{openid[-6:]}"
                db.execute("INSERT INTO users (openid, name, role, created_at) VALUES (?,?,?,?)",
                           (openid, nickname, "creator", ts))
                added += 1
        db.commit()

        return redirect(url_for("admin_users") + f"?ok=synced&added={added}&total={len(all_openids)}")

    except Exception as e:
        return redirect(url_for("admin_users") + f"?error=sync_failed&msg={str(e)[:50]}")

# ---------- API：用户管理 ----------

@app.route("/api/users", methods=["GET"])
@require_admin
def api_list_users(identity=None, client_ip=""):
    """API获取用户列表"""
    db = get_db()
    users = db.execute("SELECT id, openid, name, role, created_at FROM users ORDER BY created_at DESC").fetchall()
    return jsonify({"ok": True, "users": [dict(u) for u in users]})

@app.route("/api/users", methods=["POST"])
@require_admin
def api_add_user(identity=None, client_ip=""):
    """API添加用户"""
    data = request.get_json(force=True)
    openid = (data.get("openid") or "").strip()
    name = (data.get("name") or "").strip()
    role = (data.get("role") or "creator").strip()

    if not openid or not name:
        return jsonify({"ok": False, "error": "openid和name不能为空"}), 400
    if role not in ("admin", "creator", "reviewer"):
        return jsonify({"ok": False, "error": "角色无效，可选 admin/creator/reviewer"}), 400

    db = get_db()
    ts = now_cst()
    try:
        cursor = db.execute("INSERT INTO users (openid, name, role, created_at) VALUES (?,?,?,?)",
                            (openid, name, role, ts))
        db.commit()
        return jsonify({"ok": True, "user_id": cursor.lastrowid}), 201
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "该OpenID已存在"}), 409

@app.route("/api/users/<int:user_id>", methods=["DELETE"])
@require_admin
def api_delete_user(user_id, identity=None, client_ip=""):
    """API删除用户"""
    db = get_db()
    user = db.execute("SELECT openid FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        return jsonify({"ok": False, "error": "用户不存在"}), 404
    if identity.get("openid") == user["openid"]:
        return jsonify({"ok": False, "error": "不能删除自己"}), 400
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.commit()
    return jsonify({"ok": True})

# ---------- 微信公众号验证文件 ----------
@app.route("/MP_verify_<filename>.txt")
def wechat_verify(filename):
    """微信公众号域名验证文件"""
    import os
    verify_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        with open(os.path.join(verify_dir, f"MP_verify_{filename}.txt"), "r") as f:
            return f.read(), 200, {"Content-Type": "text/plain"}
    except FileNotFoundError:
        abort(404)

# ---------- 页面路由 ----------

@app.route("/")
def index():
    """帖子列表页"""
    db = get_db()
    page = request.args.get("page", 1, type=int)
    offset = (page - 1) * POSTS_PER_PAGE

    posts = db.execute(
        "SELECT p.*, (SELECT COUNT(*) FROM replies r WHERE r.post_id = p.id) as reply_count "
        "FROM posts p WHERE p.status = 'active' "
        "ORDER BY p.updated_at DESC LIMIT ? OFFSET ?",
        (POSTS_PER_PAGE, offset)
    ).fetchall()

    total = db.execute("SELECT COUNT(*) FROM posts WHERE status = 'active'").fetchone()[0]
    total_pages = max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)

    identity = get_current_identity()
    return render_template("index.html",
        posts=posts, page=page, total_pages=total_pages,
        identity=identity, current_ip=get_client_ip(),
        wechat_enabled=wechat_enabled())

@app.route("/post/<int:post_id>")
def view_post(post_id):
    """帖子详情+回复页"""
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        abort(404)

    page = request.args.get("page", 1, type=int)
    offset = (page - 1) * REPLIES_PER_PAGE

    replies = db.execute(
        "SELECT * FROM replies WHERE post_id = ? ORDER BY id ASC LIMIT ? OFFSET ?",
        (post_id, REPLIES_PER_PAGE, offset)
    ).fetchall()
    reply_list = []
    for r in replies:
        rd = dict(r)
        author = rd["author_name"]
        rd["role"] = "admin"
        for ip_info in ALLOWED_IPS.values():
            if ip_info["name"] == author:
                rd["role"] = ip_info.get("role", "admin")
                break
        if rd["author_openid"]:
            user = db.execute("SELECT role FROM users WHERE openid = ?", (rd["author_openid"],)).fetchone()
            if user:
                rd["role"] = user["role"]
        reply_list.append(rd)

    total = db.execute("SELECT COUNT(*) FROM replies WHERE post_id = ?", (post_id,)).fetchone()[0]
    total_pages = max(1, (total + REPLIES_PER_PAGE - 1) // REPLIES_PER_PAGE)

    identity = get_current_identity()
    show_delete_confirm = request.args.get("confirm_delete") == "1"
    return render_template("post.html",
        post=post, replies=reply_list, page=page, total_pages=total_pages,
        identity=identity, current_ip=get_client_ip(), total_replies=total,
        show_delete_confirm=show_delete_confirm, wechat_enabled=wechat_enabled())

@app.route("/post/create", methods=["POST"])
def create_post_page():
    """前端发帖"""
    identity = get_current_identity()
    if not identity:
        abort(403)

    title = request.form.get("title", "").strip()
    content = request.form.get("content", "").strip()
    tags = request.form.get("tags", "").strip()

    if not title:
        return redirect(url_for("index"))
    if len(title) > MAX_TITLE_LEN or len(content) > MAX_CONTENT_LEN or len(tags) > MAX_TAGS_LEN:
        return redirect(url_for("index"))

    db = get_db()
    ts = now_cst()
    db.execute(
        "INSERT INTO posts (title, content, tags, created_at, updated_at, author_ip, author_name, author_openid) VALUES (?,?,?,?,?,?,?,?)",
        (title, content, tags, ts, ts, get_client_ip(), identity["name"], identity.get("openid", ""))
    )
    db.commit()
    return redirect(url_for("index"))

@app.route("/post/<int:post_id>/reply", methods=["POST"])
def create_reply_page(post_id):
    """前端回复"""
    identity = get_current_identity()
    if not identity:
        abort(403)

    content = request.form.get("content", "").strip()
    if not content:
        return redirect(url_for("view_post", post_id=post_id))
    if len(content) > MAX_REPLY_LEN:
        from flask import flash
        flash(f"回复内容超长，最多允许 {MAX_REPLY_LEN} 字")
        return redirect(url_for("view_post", post_id=post_id))

    db = get_db()
    ts = now_cst()
    db.execute(
        "INSERT INTO replies (post_id, content, created_at, author_ip, author_name, author_openid) VALUES (?,?,?,?,?,?)",
        (post_id, content, ts, get_client_ip(), identity["name"], identity.get("openid", ""))
    )
    db.execute("UPDATE posts SET updated_at = ? WHERE id = ?", (ts, post_id))
    db.commit()
    return redirect(url_for("view_post", post_id=post_id))

@app.route("/post/<int:post_id>/delete", methods=["POST"])
def delete_post_page(post_id):
    """页面删除帖子（仅管理员）"""
    identity = get_current_identity()
    if not identity or identity.get("role") != "admin":
        abort(403)

    db = get_db()
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        abort(404)

    db.execute("DELETE FROM replies WHERE post_id = ?", (post_id,))
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    return redirect(url_for("index"))

# ---------- API路由 ----------

@app.route("/api/posts", methods=["GET"])
def api_list_posts():
    """获取帖子列表"""
    db = get_db()
    page = request.args.get("page", 1, type=int)
    offset = (page - 1) * POSTS_PER_PAGE

    posts = db.execute(
        "SELECT id, title, tags, created_at, updated_at, author_name, status, "
        "(SELECT COUNT(*) FROM replies r WHERE r.post_id = p.id) as reply_count "
        "FROM posts p ORDER BY updated_at DESC LIMIT ? OFFSET ?",
        (POSTS_PER_PAGE, offset)
    ).fetchall()

    total = db.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
    return jsonify({
        "ok": True,
        "posts": [dict(p) for p in posts],
        "total": total,
        "page": page,
        "pages": max(1, (total + POSTS_PER_PAGE - 1) // POSTS_PER_PAGE)
    })

@app.route("/api/posts", methods=["POST"])
@require_write
def api_create_post(identity=None, client_ip=""):
    """API创建帖子"""
    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    tags = (data.get("tags") or "").strip()

    if not title:
        return jsonify({"ok": False, "error": "标题不能为空"}), 400
    if len(title) > MAX_TITLE_LEN:
        return jsonify({"ok": False, "error": f"标题过长，最多 {MAX_TITLE_LEN} 字"}), 400
    if len(content) > MAX_CONTENT_LEN:
        return jsonify({"ok": False, "error": f"内容过长，最多 {MAX_CONTENT_LEN} 字"}), 400
    if len(tags) > MAX_TAGS_LEN:
        return jsonify({"ok": False, "error": f"标签过长，最多 {MAX_TAGS_LEN} 字"}), 400

    db = get_db()
    ts = now_cst()
    cursor = db.execute(
        "INSERT INTO posts (title, content, tags, created_at, updated_at, author_ip, author_name, author_openid) VALUES (?,?,?,?,?,?,?,?)",
        (title, content, tags, ts, ts, client_ip, identity["name"], identity.get("openid", ""))
    )
    db.commit()
    return jsonify({"ok": True, "post_id": cursor.lastrowid}), 201

@app.route("/api/posts/<int:post_id>", methods=["GET"])
def api_get_post(post_id):
    """获取帖子详情"""
    db = get_db()
    post = db.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        return jsonify({"ok": False, "error": "帖子不存在"}), 404
    return jsonify({"ok": True, "post": dict(post)})

@app.route("/api/posts/<int:post_id>", methods=["DELETE"])
@require_admin
def api_delete_post(post_id, identity=None, client_ip=""):
    """API删除帖子（仅管理员）"""
    db = get_db()
    post = db.execute("SELECT id, title FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        return jsonify({"ok": False, "error": "帖子不存在"}), 404
    db.execute("DELETE FROM replies WHERE post_id = ?", (post_id,))
    db.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    db.commit()
    return jsonify({"ok": True, "message": f"已删除帖子「{post['title']}」"})

@app.route("/api/posts/<int:post_id>/replies", methods=["GET"])
def api_list_replies(post_id):
    """获取某帖所有回复"""
    db = get_db()
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        return jsonify({"ok": False, "error": "帖子不存在"}), 404

    since_id = request.args.get("since_id", 0, type=int)
    replies = db.execute(
        "SELECT * FROM replies WHERE post_id = ? AND id > ? ORDER BY id ASC",
        (post_id, since_id)
    ).fetchall()

    return jsonify({
        "ok": True,
        "post_id": post_id,
        "replies": [dict(r) for r in replies],
        "has_new": len(replies) > 0
    })

@app.route("/api/posts/<int:post_id>/replies", methods=["POST"])
@require_write
def api_create_reply(post_id, identity=None, client_ip=""):
    """API回复帖子"""
    data = request.get_json(force=True)
    content = (data.get("content") or "").strip()

    if not content:
        return jsonify({"ok": False, "error": "内容不能为空"}), 400
    if len(content) > MAX_REPLY_LEN:
        return jsonify({"ok": False, "error": f"回复超长，最多允许 {MAX_REPLY_LEN} 字"}), 413

    db = get_db()
    post = db.execute("SELECT id FROM posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        return jsonify({"ok": False, "error": "帖子不存在"}), 404

    ts = now_cst()
    cursor = db.execute(
        "INSERT INTO replies (post_id, content, created_at, author_ip, author_name, author_openid) VALUES (?,?,?,?,?,?)",
        (post_id, content, ts, client_ip, identity["name"], identity.get("openid", ""))
    )
    db.execute("UPDATE posts SET updated_at = ? WHERE id = ?", (ts, post_id))
    db.commit()
    return jsonify({
        "ok": True,
        "reply_id": cursor.lastrowid,
        "reply": {
            "id": cursor.lastrowid,
            "content": content,
            "created_at": ts,
            "author_name": identity["name"]
        }
    }), 201

@app.route("/api/posts/<int:post_id>/replies/latest", methods=["GET"])
def api_latest_reply_id(post_id):
    """获取某帖最新回复ID（用于轮询判断有无新内容）"""
    db = get_db()
    row = db.execute(
        "SELECT MAX(id) as max_id FROM replies WHERE post_id = ?", (post_id,)
    ).fetchone()
    return jsonify({"ok": True, "post_id": post_id, "latest_reply_id": row["max_id"] or 0})

@app.route("/api/whoami", methods=["GET"])
def api_whoami():
    """查看当前身份"""
    identity = get_current_identity()
    if identity:
        return jsonify({
            "ok": True,
            "ip": get_client_ip(),
            "name": identity["name"],
            "role": identity["role"],
            "source": identity.get("source", "unknown"),
            "can_write": True
        })
    return jsonify({"ok": True, "ip": get_client_ip(), "can_write": False})

# ---------- 启动 ----------

if __name__ == "__main__":
    init_db()
    print(f"[agent-forum] http://{HOST}:{PORT}")
    print(f"   allowed IPs: {list(ALLOWED_IPS.keys())}")
    print(f"   wechat login: {'enabled' if wechat_enabled() else 'disabled'}")
    print(f"   database: {DATABASE}")
    app.run(host=HOST, port=PORT, debug=DEBUG)
