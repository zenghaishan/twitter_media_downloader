import os
import secrets
import threading
from flask import Flask

from config import Config
from routes import main_bp
from routes_auth import auth_bp
from routes_profile import profile_bp
from routes_admin import admin_bp
import database
import webdav_sync


def get_or_create_secret_key() -> str:
    """获取或创建持久化的secret_key"""
    database.init_db()
    # secret_key 是全局配置，使用 user_id=0 表示
    key = database.get_config('secret_key', user_id=0)
    if not key:
        key = secrets.token_hex(32)
        # 使用 update_config 来插入或更新
        database.update_config('secret_key', key, user_id=0)
    return key


def create_app() -> Flask:
    """创建Flask应用"""
    app = Flask(__name__)
    
    # 配置应用
    app.config['MAX_CONTENT_LENGTH'] = Config.MAX_CONTENT_LENGTH
    app.config['DOWNLOAD_FOLDER'] = Config.DOWNLOAD_FOLDER
    
    # Session配置 - 使用持久化的secret_key
    app.secret_key = get_or_create_secret_key()
    app.config['SESSION_TYPE'] = 'filesystem'
    app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30  # 30天
    
    # 注册蓝图
    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(profile_bp)
    app.register_blueprint(admin_bp)
    
    return app


app = create_app()


def _webdav_startup_pull():
    """服务启动时若已配置 WebDAV，后台拉取还原（不阻塞启动）"""
    try:
        if webdav_sync.is_configured():
            result = webdav_sync.pull_all()
            if result.get('restored_config') or result.get('restored_history'):
                print(f"[webdav] 已从云端还原配置/历史 {result}", flush=True)
    except Exception:
        import traceback
        traceback.print_exc()


threading.Thread(target=_webdav_startup_pull, daemon=True).start()

if __name__ == '__main__':
    # use_reloader=False：关闭 debug 热重载，避免重载时丢失下载 worker 线程与内存队列，
    # 导致任务永远停留在 queued。改代码后需手动重启服务。
    app.run(
        debug=Config.DEBUG,
        host=Config.HOST,
        port=Config.PORT,
        use_reloader=False
    )