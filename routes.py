import os
import glob
import zipfile
import asyncio
import re
import json
import time
import queue
import httpx
from datetime import datetime
from functools import wraps
from flask import Blueprint, render_template, request, jsonify, send_file, send_from_directory, g, Response, after_this_request

from config import Config
from services import download_service
from realtime_logger import log_manager
import database
from auth import login_required, get_current_user, admin_required
import webdav_sync

# 媒体文件扩展名
MEDIA_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.mp4', '.webm', '.webp'}

# ZIP文件缓存：{uuid: {'zip_path': str, 'zip_filename': str, 'created_at': float}}
zip_cache = {}
CACHE_EXPIRY = 3600  # 缓存过期时间（秒）

def resolve_media_dir(user_id: str, account_user_id: int = None) -> str:
    """定位某用户的真实媒体目录。
    优先：可配置 download_dir 下的“昵称(user_id)”子目录；
    其次：download_dir 下同名目录；
    回退：默认下载目录/<user_id>。
    """
    dl = database.get_config('download_dir', account_user_id)
    root = dl.strip() if dl and dl.strip() else Config.DOWNLOAD_FOLDER
    if os.path.isdir(root):
        for name in os.listdir(root):
            p = os.path.join(root, name)
            if os.path.isdir(p) and f'({user_id})' in name:
                return p
        direct = os.path.join(root, user_id)
        if os.path.isdir(direct):
            return direct
    return os.path.join(Config.DOWNLOAD_FOLDER, user_id)


def count_media_files(user_id: str, account_user_id: int = None) -> int:
    """统计用户文件夹中的实际媒体文件数量"""
    user_folder = resolve_media_dir(user_id, account_user_id)
    if not os.path.isdir(user_folder):
        return 0
    
    count = 0
    for ext in MEDIA_EXTENSIONS:
        count += len(glob.glob(os.path.join(user_folder, f'*{ext}')))
    return count


def get_folder_size(user_id: str, account_user_id: int = None) -> int:
    """获取用户文件夹的实际总大小"""
    user_folder = resolve_media_dir(user_id, account_user_id)
    if not os.path.isdir(user_folder):
        return 0
    
    total_size = 0
    for ext in MEDIA_EXTENSIONS:
        for f in glob.glob(os.path.join(user_folder, f'*{ext}')):
            if os.path.isfile(f):
                total_size += os.path.getsize(f)
    return total_size

# 创建蓝图
main_bp = Blueprint('main', __name__)


@main_bp.route('/')
@login_required
def index():
    """首页"""
    return send_file(os.path.join(Config.RESOURCE_DIR, 'vue/dist/index.html'))


@main_bp.route('/config')
@login_required
def config_page():
    """配置管理页面"""
    return send_file(os.path.join(Config.RESOURCE_DIR, 'vue/dist/index.html'))


@main_bp.route('/history')
@login_required
def history_page():
    """下载历史页面"""
    return send_file(os.path.join(Config.RESOURCE_DIR, 'vue/dist/index.html'))


@main_bp.route('/detail/<user_id>')
@login_required
def detail_page(user_id: str):
    """用户媒体详情页面"""
    return send_file(os.path.join(Config.RESOURCE_DIR, 'vue/dist/index.html'))


@main_bp.route('/gallery')
@login_required
def gallery_page():
    """用户画廊页面"""
    return send_file(os.path.join(Config.RESOURCE_DIR, 'vue/dist/index.html'))


@main_bp.route('/assets/<path:filename>')
def serve_vue_assets(filename):
    """服务Vue应用的静态资源"""
    return send_from_directory(os.path.join(Config.RESOURCE_DIR, 'vue/dist/assets'), filename)


@main_bp.route('/api/download', methods=['POST'])
@login_required
def start_download():
    """开始下载"""
    data = request.get_json()
    user_ids = data.get('user_id', '').strip()
    download_type = data.get('download_type', 'all')  # all, video, image
    export_xlsx = data.get('export_xlsx', False)  # 是否导出xlsx
    create_zip = data.get('create_zip', False)  # 是否生成压缩包
    force = data.get('force', False)  # 是否强制重新下载
    
    if not user_ids:
        return jsonify({'error': '请输入用户ID'}), 400
    
    if download_type not in ['all', 'video', 'image']:
        return jsonify({'error': '无效的下载类型'}), 400
    
    # 支持多个用户ID，用逗号分隔
    user_id_list = [uid.strip() for uid in user_ids.split(',') if uid.strip()]
    
    if not user_id_list:
        return jsonify({'error': '请输入有效的用户ID'}), 400
    
    # 获取当前登录用户ID
    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None
    
    # 创建下载任务
    tasks = []
    for user_id in user_id_list:
        task = download_service.create_task(user_id, download_type, account_user_id, export_xlsx=export_xlsx, create_zip=create_zip, force=force)
        tasks.append({
            'task_id': task.task_id,
            'user_id': user_id
        })
    
    if len(tasks) == 1:
        return jsonify({
            'task_id': tasks[0]['task_id'],
            'queue_size': download_service.queue_size(),
            'message': f'开始下载用户 {tasks[0]["user_id"]} 的媒体文件'
        })
    else:
        return jsonify({
            'tasks': tasks,
            'queue_size': download_service.queue_size(),
            'message': f'已创建 {len(tasks)} 个下载任务'
        })


@main_bp.route('/api/download-share', methods=['POST'])
@login_required
def start_download_share():
    """通过分享链接下载：解析推文/用户链接并创建下载任务"""
    data = request.get_json() or {}
    share_url = (data.get('url') or '').strip()
    if not share_url:
        return jsonify({'error': '请输入分享链接'}), 400

    # 匹配推文链接 https://x.com/<user>/status/<id>
    share_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)/status/(\d+)', share_url)
    if share_re:
        screen_name = share_re.group(1)
        status_id = share_re.group(2)
    else:
        # 匹配用户主页链接 https://x.com/<user>
        user_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)', share_url)
        if not user_re:
            return jsonify({'error': '无法识别的链接，请提供 twitter.com 或 x.com 的分享链接'}), 400
        screen_name = user_re.group(1)
        status_id = None

    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None
    export_xlsx = data.get('export_xlsx', False)
    create_zip = data.get('create_zip', False)
    force = data.get('force', False)  # 是否强制重新下载

    if status_id:
        print(f"[download-share] create_task enter: user={screen_name} single_tweet={status_id} force={force}", flush=True)
        task = download_service.create_task(
            screen_name, 'all', account_user_id,
            export_xlsx=export_xlsx, create_zip=create_zip,
            single_tweet=status_id, force=force, link=share_url
        )
        print(f"[download-share] create_task done: task_id={task.task_id}", flush=True)
        return jsonify({
            'task_id': task.task_id,
            'user_id': screen_name,
            'queue_size': download_service.queue_size(),
            'message': f'正在下载推文 {status_id} 的媒体（用户 @{screen_name}）'
        })
    else:
        task = download_service.create_task(
            screen_name, 'all', account_user_id,
            export_xlsx=export_xlsx, create_zip=create_zip,
            force=force, link=share_url
        )
        return jsonify({
            'task_id': task.task_id,
            'user_id': screen_name,
            'queue_size': download_service.queue_size(),
            'message': f'开始下载用户 @{screen_name} 的媒体文件'
        })


@main_bp.route('/api/re-download', methods=['POST'])
@login_required
def re_download():
    """重新下载某条历史/队列任务的链接（强制重新下载，已提交过的链接会提到队首）"""
    data = request.get_json() or {}
    task_id = (data.get('task_id') or '').strip()
    if not task_id:
        return jsonify({'error': '缺少任务ID'}), 400

    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None

    task = download_service.get_task(task_id)
    if not task:
        return jsonify({'error': '任务不存在或已被清理'}), 404

    try:
        new_task = download_service.re_download_task(task, account_user_id)
    except Exception as e:
        return jsonify({'error': f'重新下载失败: {str(e)}'}), 500

    return jsonify({
        'task_id': new_task.task_id,
        'user_id': new_task.user_id,
        'queue_size': download_service.queue_size(),
        'message': f'已重新入队下载 @{new_task.user_id} 的链接'
    })


def parse_share_url(share_url: str):
    """解析分享链接，返回 (screen_name, status_id|None)。无法识别返回 (None, None)。"""
    share_url = (share_url or '').strip()
    if not share_url:
        return None, None
    share_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)/status/(\d+)', share_url)
    if share_re:
        return share_re.group(1), share_re.group(2)
    user_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)', share_url)
    if user_re:
        return user_re.group(1), None
    return None, None


@main_bp.route('/api/preview-media', methods=['POST'])
@login_required
def preview_media():
    """抓取媒体列表（不下载），供选择性下载勾选。"""
    data = request.get_json() or {}
    share_url = (data.get('url') or '').strip()
    if not share_url:
        return jsonify({'error': '请输入链接'}), 400

    screen_name, status_id = parse_share_url(share_url)
    if not screen_name:
        return jsonify({'error': '无法识别的链接，请提供 twitter.com 或 x.com 的分享链接'}), 400

    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None

    try:
        result = download_service.preview_media(screen_name, status_id, account_user_id)
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': f'抓取媒体列表失败: {str(e)}'}), 500


@main_bp.route('/api/download-selected', methods=['POST'])
@login_required
def download_selected():
    """选择性下载：入队下载用户勾选的媒体项。"""
    data = request.get_json() or {}
    user_id = (data.get('user_id') or '').strip()
    items = data.get('items') or []
    force = data.get('force', False)

    if not user_id:
        return jsonify({'error': '缺少用户'}), 400
    if not items:
        return jsonify({'error': '未选择任何文件'}), 400

    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None

    task = download_service.create_selected_task(user_id, items, account_user_id, force=force)
    return jsonify({
        'task_id': task.task_id,
        'user_id': user_id,
        'count': len(items),
        'queue_size': download_service.queue_size(),
        'message': f'已入队下载 {len(items)} 个文件'
    })


@main_bp.route('/api/progress/<task_id>')
def get_progress(task_id: str):
    """获取下载进度"""
    task = download_service.get_task(task_id)
    
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    
    return jsonify(task.to_dict())


@main_bp.route('/api/download/<task_id>')
def download_file(task_id: str):
    """下载文件"""
    task = download_service.get_task(task_id)
    
    if not task:
        return jsonify({'error': '任务不存在'}), 404
    
    if task.status != 'completed':
        return jsonify({'error': '下载尚未完成'}), 400
    
    if not task.zip_path or not os.path.exists(task.zip_path):
        return jsonify({'error': '压缩文件不存在'}), 404
    
    # 从 zip_path 获取文件名
    download_name = os.path.basename(task.zip_path)
    
    return send_file(
        task.zip_path,
        as_attachment=True,
        download_name=download_name
    )


@main_bp.route('/api/zip/<user_id>', methods=['POST'])
@login_required
def create_user_zip(user_id: str):
    """为指定用户创建ZIP压缩包"""
    try:
        # 获取可选的UUID参数
        uuid = request.args.get('uuid')
        
        # 获取用户信息
        history = database.get_latest_download_by_user_id(user_id)
        user_name = history.get('user_name') if history else None
        # 清理用户名中不安全的文件系统字符
        if user_name:
            user_name = re.sub(r'[/\\:*?"<>|]', '_', user_name).strip()
        name_prefix = f'{user_name}_{user_id}' if user_name else user_id
        
        # 生成ZIP文件名
        if uuid:
            # 使用UUID作为文件名的一部分，确保相同UUID生成相同文件名
            zip_filename = f'{name_prefix}_video_img_{uuid}.zip'
        else:
            # 没有UUID，使用时间戳
            timestamp = time.strftime('%Y%m%d_%H%M%S')
            zip_filename = f'{name_prefix}_video_img_{timestamp}.zip'
        
        zip_path = os.path.join(Config.DOWNLOAD_FOLDER, zip_filename)
        
        # 如果文件已存在，直接返回
        if os.path.exists(zip_path):
            # 如果提供了UUID，更新缓存
            if uuid:
                zip_cache[uuid] = {
                    'zip_path': zip_path,
                    'zip_filename': zip_filename,
                    'created_at': time.time()
                }
            return jsonify({
                'message': 'ZIP文件已存在',
                'zip_filename': zip_filename,
                'download_url': f'/api/download-zip/{zip_filename}'
            })
        
        user_dir = os.path.join(Config.DOWNLOAD_FOLDER, user_id)
        
        if not os.path.exists(user_dir) or not os.path.isdir(user_dir):
            return jsonify({'error': '用户文件夹不存在'}), 404
        
        # 检查是否有媒体文件
        media_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.mp4', '.mov', '.avi', '.mkv', '.webm'}
        has_media = False
        for filename in os.listdir(user_dir):
            ext = os.path.splitext(filename)[1].lower()
            if ext in media_extensions:
                has_media = True
                break
        
        if not has_media:
            return jsonify({'error': '用户文件夹中没有媒体文件'}), 400
        
        # 创建ZIP文件
        video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.gif'}
        image_exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
        
        # 先创建临时文件，完成后再重命名
        temp_zip_path = zip_path + '.tmp'
        try:
            with zipfile.ZipFile(temp_zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                for filename in os.listdir(user_dir):
                    filepath = os.path.join(user_dir, filename)
                    if not os.path.isfile(filepath):
                        continue
                    
                    ext = os.path.splitext(filename)[1].lower()
                    if ext not in media_extensions:
                        continue
                    
                    # 分类到子文件夹
                    if ext in video_exts:
                        arcname = f'{name_prefix}/videos/{filename}'
                    elif ext in image_exts:
                        arcname = f'{name_prefix}/images/{filename}'
                    else:
                        arcname = f'{name_prefix}/others/{filename}'
                    
                    zipf.write(filepath, arcname)
            
            # 完成后重命名
            os.rename(temp_zip_path, zip_path)
            
        except Exception as e:
            # 清理临时文件
            if os.path.exists(temp_zip_path):
                os.remove(temp_zip_path)
            raise e
        
        # 如果提供了UUID，缓存结果
        if uuid:
            zip_cache[uuid] = {
                'zip_path': zip_path,
                'zip_filename': zip_filename,
                'created_at': time.time()
            }
        
        # 返回下载链接
        return jsonify({
            'message': 'ZIP文件创建成功',
            'zip_filename': zip_filename,
            'download_url': f'/api/download-zip/{zip_filename}'
        })
        
    except Exception as e:
        return jsonify({'error': f'创建ZIP文件失败: {str(e)}'}), 500


@main_bp.route('/api/download-zip/<filename>')
@login_required
def download_zip(filename: str):
    """下载ZIP文件"""
    import threading

    zip_path = os.path.join(Config.DOWNLOAD_FOLDER, filename)

    if not os.path.exists(zip_path):
        return jsonify({'error': '文件不存在'}), 404

    # 获取删除延迟时间配置（默认20分钟）
    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None
    delete_delay = database.get_config('zip_delete_delay', account_user_id)
    delete_minutes = int(delete_delay) if delete_delay else 20

    # 定时删除ZIP文件
    def delete_zip_later():
        import time
        time.sleep(delete_minutes * 60)
        try:
            if os.path.exists(zip_path):
                os.remove(zip_path)
                print(f'已删除ZIP文件: {filename}')
        except Exception as e:
            print(f'删除ZIP文件失败: {e}')

    threading.Thread(target=delete_zip_later, daemon=True).start()

    # 用 send_file 处理流式传输：请求结束或连接中断时会自动释放文件句柄，
    # 避免手写 generate() 在断连时泄漏句柄、导致ZIP文件被Python进程锁死。
    # mimetype 固定为标准的 application/zip，保证浏览器正确按ZIP附件识别。
    return send_file(
        zip_path,
        as_attachment=True,
        download_name=os.path.basename(zip_path),
        mimetype='application/zip',
        conditional=True
    )


# 配置管理API
@main_bp.route('/api/configs')
@login_required
def get_configs():
    """获取当前用户的配置"""
    current_user = get_current_user()
    if not current_user:
        return jsonify({'error': '未登录'}), 401
    
    configs = database.get_all_configs(user_id=current_user['id'])
    # 过滤掉系统配置项
    configs = [c for c in configs if c['key'] != 'secret_key']
    return jsonify(configs)


@main_bp.route('/api/configs', methods=['PUT'])
@login_required
def update_configs():
    """更新当前用户的配置"""
    current_user = get_current_user()
    if not current_user:
        return jsonify({'error': '未登录'}), 401
    
    data = request.get_json()
    
    if not data:
        return jsonify({'error': '无效的数据'}), 400
    
    try:
        for key, value in data.items():
            database.update_config(key, value, user_id=current_user['id'])
        # 配置变更后，若 WebDAV 已启用则异步推送
        if webdav_sync.is_configured():
            webdav_sync.push_async()
        return jsonify({'message': '配置已更新'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# WebDAV 同步
@main_bp.route('/api/webdav/test', methods=['POST'])
@login_required
def webdav_test_route():
    """测试 WebDAV 连接"""
    try:
        ok = webdav_sync.test_connection()
        if not ok:
            return jsonify({'ok': False, 'error': '无法连接，请检查地址/账号/密码/目录权限'}), 400
        return jsonify({'ok': True, 'message': 'WebDAV 连接成功'})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 400


@main_bp.route('/api/webdav/sync', methods=['POST'])
@login_required
def webdav_sync_route():
    """手动推送/拉取 WebDAV 同步"""
    data = request.get_json() or {}
    action = data.get('action', 'push')
    try:
        if action == 'push':
            r = webdav_sync.push_all()
        elif action == 'pull':
            r = webdav_sync.pull_all()
        else:
            return jsonify({'ok': False, 'error': '未知操作'}), 400
        return jsonify({'ok': True, **r})
    except webdav_sync.WebDAVError as e:
        return jsonify({'ok': False, 'error': str(e)}), 400
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500


# 下载历史API
@main_bp.route('/api/history')
@login_required
def get_history():
    """获取下载历史"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    keyword = request.args.get('keyword', '').strip()
    status = request.args.get('status', '').strip()
    date = request.args.get('date', '').strip()
    
    offset = (page - 1) * per_page
    
    # 获取当前用户信息，用于数据隔离
    current_user = get_current_user()
    account_user_id = None
    if current_user and current_user['role'] != 'admin':
        # 普通用户只能看到自己的记录
        account_user_id = current_user['id']
    
    history = database.get_download_history(
        limit=per_page, 
        offset=offset,
        keyword=keyword,
        status=status,
        date=date,
        account_user_id=account_user_id
    )
    total = database.get_download_history_count(
        keyword=keyword,
        status=status,
        date=date,
        account_user_id=account_user_id
    )
    
    # 检查本地是否存在ZIP文件，如果不存在则清空zip_path
    for item in history:
        if item.get('zip_path') and not os.path.exists(item['zip_path']):
            item['zip_path'] = None
    
    # 计算每个用户文件夹的实际大小
    user_folder_sizes = {}
    for item in history:
        user_id = item.get('user_id')
        if user_id and user_id not in user_folder_sizes:
            user_folder_sizes[user_id] = get_folder_size(user_id, account_user_id)
        item['folder_size'] = user_folder_sizes.get(user_id, 0)

    # 补全缺失的昵称：任务中断（如服务重启清理）时未写入用户信息，
    # 回填该用户最近一条有效昵称；头像统一由前端经 /api/avatar 拉取本地缓存，
    # 这里不回填可能失效的 Twitter 外链，避免 img 加载失败导致头像全无。
    need_backfill = [i for i in history if i.get('user_id') and not i.get('user_name')]
    if need_backfill:
        ref_cache = {}
        for i in need_backfill:
            uid = i['user_id']
            if uid not in ref_cache:
                ref_cache[uid] = database.get_download_history_by_user_id(uid)
            for r in ref_cache[uid]:
                if r.get('user_name'):
                    i['user_name'] = r['user_name']
                    break

    # 头像统一指向本地缓存文件（若该用户本地有头像）：
    # 空头像或在“外链”的，只要本地有缓存就填 /api/avatar-file/{uid}，
    # 保证 img 可显示，且前后端返回稳定一致，避免列表不停刷新。
    avatar_dir = os.path.join(Config.BASE_DIR, 'downloads', '.avatars')
    _avatar_exts = ['.jpg', '.jpeg', '.png', '.webp']
    for item in history:
        uid = item.get('user_id')
        if not uid:
            continue
        av = item.get('avatar_url')
        if not av or (isinstance(av, str) and av.startswith('http')):
            if any(os.path.exists(os.path.join(avatar_dir, f'{uid}{e}')) for e in _avatar_exts):
                item['avatar_url'] = f'/api/avatar-file/{uid}'

    return jsonify({
        'data': history,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })


@main_bp.route('/api/history/<task_id>', methods=['DELETE'])
def delete_history(task_id: str):
    """删除下载历史"""
    try:
        import shutil
        
        # 先获取下载记录，获取用户ID
        history = database.get_download_by_task_id(task_id)
        if history and history.get('user_id'):
            user_id = history['user_id']
            user_dir = os.path.join(Config.DOWNLOAD_FOLDER, user_id)
            
            # 检查是否还有其他下载记录使用同一个用户目录
            other_records = database.get_download_history_by_user_id(user_id)
            # 排除当前要删除的记录
            other_records = [r for r in other_records if r['task_id'] != task_id]
            
            # 如果没有其他记录使用这个用户目录，则删除
            if not other_records and os.path.exists(user_dir):
                shutil.rmtree(user_dir)
        
        # 同步从内存队列移除，避免删除后仍在队列面板显示“排队中”
        download_service.remove_task(task_id)
        database.delete_download_history(task_id)
        return jsonify({'message': '已删除'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# 用户画廊API
@main_bp.route('/api/gallery/users')
@login_required
def get_gallery_users():
    """获取画廊用户列表"""
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 20, type=int)
    keyword = request.args.get('keyword', '').strip()

    offset = (page - 1) * per_page

    current_user = get_current_user()
    account_user_id = None
    if current_user and current_user['role'] != 'admin':
        account_user_id = current_user['id']

    users = database.get_gallery_users(
        keyword=keyword,
        limit=per_page,
        offset=offset,
        account_user_id=account_user_id
    )
    total = database.get_gallery_users_count(
        keyword=keyword,
        account_user_id=account_user_id
    )

    # 使用文件夹中的实际媒体文件数量和大小替代数据库统计
    for user in users:
        user['total_files'] = count_media_files(user['user_id'], account_user_id)
        user['total_size'] = get_folder_size(user['user_id'], account_user_id)

    return jsonify({
        'data': users,
        'total': total,
        'page': page,
        'per_page': per_page,
        'total_pages': (total + per_page - 1) // per_page
    })


@main_bp.route('/api/cache', methods=['DELETE'])
def clear_cache():
    """清理所有缓存文件"""
    try:
        result = download_service.clear_all_cache()
        return jsonify({
            'message': f'已清理 {result["deleted_files"]} 个文件和 {result["deleted_dirs"]} 个目录',
            'deleted_files': result['deleted_files'],
            'deleted_dirs': result['deleted_dirs']
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


async def _fetch_user_avatar(user_id: str, account_user_id: int = None) -> str:
    """异步获取用户头像URL"""
    proxy = Config.get_proxy(user_id=account_user_id)
    cookie = Config.get_cookie(user_id=account_user_id)
    
    headers = {
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
        'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
        'cookie': cookie
    }
    
    re_token = r'ct0=([a-f0-9]+)'
    match = re.search(re_token, cookie)
    if match:
        headers['x-csrf-token'] = match.group(1)
    
    headers['referer'] = f'https://twitter.com/{user_id}'
    
    url = f'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={{"screen_name":"{user_id}","withSafetyModeUserFields":false}}&features={{"hidden_profile_likes_enabled":false,"hidden_profile_subscriptions_enabled":false,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"subscriptions_verification_info_verified_since_enabled":true,"highlights_tweets_tab_ui_enabled":true,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true}}&fieldToggles={{"withAuxiliaryUserLabels":false}}'
    
    _proxy = proxy if proxy and proxy.strip() else None
    
    async with httpx.AsyncClient(proxy=_proxy) as client:
        response = await client.get(url.replace('{', '%7B').replace('}', '%7D'), headers=headers, timeout=10.0)
        if response.status_code == 200:
            data = response.json()
            if 'data' in data:
                user_result = data['data']['user']['result']
                avatar_url = user_result.get('legacy', {}).get('profile_image_url_https')
                # 将头像URL替换为高清版本 _400x400
                if avatar_url:
                    avatar_url = avatar_url.replace('_normal', '_400x400')
                return avatar_url
    return None


async def _download_avatar(url: str, save_path: str, account_user_id: int = None) -> bool:
    """下载头像并保存到本地"""
    try:
        proxy = Config.get_proxy(user_id=account_user_id)
        _proxy = proxy if proxy and proxy.strip() else None
        
        async with httpx.AsyncClient(proxy=_proxy) as client:
            response = await client.get(url, timeout=15.0)
            if response.status_code == 200:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, 'wb') as f:
                    f.write(response.content)
                return True
    except Exception:
        pass
    return False


async def _fetch_thumb(url: str, account_user_id: int = None):
    """通过配置的代理抓取 Twitter 封面图字节，供本地浏览器直接展示"""
    try:
        proxy = Config.get_proxy(user_id=account_user_id)
        cookie = Config.get_cookie(user_id=account_user_id)
        _proxy = proxy if proxy and proxy.strip() else None
        headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
            'referer': 'https://x.com/',
        }
        # 带上用户的 ct0/x-csrf，提高视频海报帧等受保护资源的抓取成功率
        if cookie:
            headers['cookie'] = cookie
            m = re.search(r'ct0=([a-f0-9]+)', cookie)
            if m:
                headers['x-csrf-token'] = m.group(1)
        async with httpx.AsyncClient(proxy=_proxy, timeout=20.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                return response.content
    except Exception:
        pass
    return None


@main_bp.route('/api/thumb')
@login_required
def media_thumb():
    """本地代理 Twitter 封面/图片，解决浏览器无法直连 CDN 导致封面不显示的问题"""
    url = (request.args.get('url') or '').strip()
    if not url:
        return jsonify({'error': '缺少url'}), 400
    # 仅允许加载 Twitter 媒体 CDN，避免被当作任意代理(SSRF)
    if not (url.startswith('https://pbs.twimg.com/') or url.startswith('https://video.twimg.com/')):
        return jsonify({'error': '非法的图片地址'}), 400

    current_user = get_current_user()
    account_user_id = current_user['id'] if current_user else None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        data = loop.run_until_complete(_fetch_thumb(url, account_user_id))
    finally:
        loop.close()
    if data is None:
        return jsonify({'error': '获取封面失败'}), 502

    if '.png' in url.lower():
        mime = 'image/png'
    elif '.gif' in url.lower():
        mime = 'image/gif'
    elif '.webp' in url.lower():
        mime = 'image/webp'
    else:
        mime = 'image/jpeg'
    resp = Response(data, mimetype=mime)
    resp.headers['Cache-Control'] = 'public, max-age=86400'
    return resp


@main_bp.route('/api/avatar/<user_id>')
@login_required
def get_user_avatar(user_id: str):
    """获取用户头像并保存到本地"""
    try:
        current_user = get_current_user()
        account_user_id = current_user['id'] if current_user else None
        
        # 检查本地是否已有头像
        avatar_dir = os.path.join(Config.BASE_DIR, 'downloads', '.avatars')
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            local_path = os.path.join(avatar_dir, f'{user_id}{ext}')
            if os.path.exists(local_path):
                return jsonify({'avatar_url': f'/api/avatar-file/{user_id}'})
        
        # 从 Twitter 获取头像 URL
        loop = asyncio.new_event_loop()
        avatar_url = loop.run_until_complete(_fetch_user_avatar(user_id, account_user_id=account_user_id))
        loop.close()
        
        if avatar_url:
            # 下载并保存到本地
            ext = '.jpg'
            if '.png' in avatar_url:
                ext = '.png'
            elif '.webp' in avatar_url:
                ext = '.webp'
            
            save_path = os.path.join(avatar_dir, f'{user_id}{ext}')
            loop = asyncio.new_event_loop()
            success = loop.run_until_complete(_download_avatar(avatar_url, save_path, account_user_id))
            loop.close()
            
            if success:
                local_url = f'/api/avatar-file/{user_id}'
                database.update_avatar_by_user_id(user_id, local_url)
                return jsonify({'avatar_url': local_url})
        
        return jsonify({'avatar_url': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/avatar-file/<user_id>')
@login_required
def serve_avatar_file(user_id: str):
    """提供本地头像文件访问"""
    try:
        avatar_dir = os.path.join(Config.BASE_DIR, 'downloads', '.avatars')
        
        # 查找头像文件
        for ext in ['.jpg', '.jpeg', '.png', '.webp']:
            file_path = os.path.join(avatar_dir, f'{user_id}{ext}')
            if os.path.exists(file_path):
                return send_file(file_path)
        
        return jsonify({'error': '头像不存在'}), 404
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== 用户媒体详情API ====================

def _generate_video_thumbnail(video_path: str, thumb_path: str) -> bool:
    """使用ffmpeg生成视频缩略图"""
    try:
        import subprocess
        # 确保缩略图目录存在
        os.makedirs(os.path.dirname(thumb_path), exist_ok=True)
        # 使用ffmpeg提取第1秒的帧作为缩略图
        cmd = [
            'ffmpeg', '-i', video_path,
            '-ss', '00:00:01',
            '-vframes', '1',
            '-vf', 'scale=320:-1',
            '-y',
            thumb_path
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        return result.returncode == 0 and os.path.exists(thumb_path)
    except Exception:
        return False


@main_bp.route('/api/user-media/<user_id>')
@login_required
def get_user_media(user_id: str):
    """获取指定用户的本地媒体文件列表"""
    try:
        from config import Config
        
        # 分页参数
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', 24, type=int)
        file_type = request.args.get('type', 'all').strip()  # all, image, video
        
        # 构建用户目录路径（兼容自定义 download_dir + 昵称(user_id) 子目录）
        current_user = get_current_user()
        account_user_id = current_user['id'] if current_user else None
        user_dir = resolve_media_dir(user_id, account_user_id)
        thumb_dir = os.path.join(Config.BASE_DIR, 'downloads', '.thumbnails', user_id)
        
        if not os.path.exists(user_dir):
            return jsonify({
                'user_id': user_id,
                'files': [],
                'total': 0,
                'images': 0,
                'videos': 0,
                'page': page,
                'per_page': per_page,
                'total_pages': 0
            })
        
        # 获取所有媒体文件
        image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        video_extensions = {'.mp4', '.mov', '.avi', '.mkv', '.webm'}
        
        all_files = []
        image_count = 0
        video_count = 0
        
        for filename in os.listdir(user_dir):
            filepath = os.path.join(user_dir, filename)
            if not os.path.isfile(filepath):
                continue
            
            ext = os.path.splitext(filename)[1].lower()
            file_type_detected = None
            
            if ext in image_extensions:
                file_type_detected = 'image'
                image_count += 1
            elif ext in video_extensions:
                file_type_detected = 'video'
                video_count += 1
            else:
                continue
            
            # 获取文件信息
            stat = os.stat(filepath)
            
            # 从文件名解析日期 (格式: YYYY-MM-DD HH-MM-type_id.ext)
            date_str = ''
            parts = filename.split(' ')
            if len(parts) >= 2:
                date_str = parts[0] + ' ' + parts[1].split('-')[0] + ':' + parts[1].split('-')[1] if len(parts[1].split('-')) >= 2 else ''
            
            file_info = {
                'name': filename,
                'path': f'/api/media-file/{user_id}/{filename}',
                'type': file_type_detected,
                'size': stat.st_size,
                'date': date_str,
                'modified': datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
                'thumb': None
            }
            
            all_files.append(file_info)
        
        # 按文件名倒序排列（文件名包含日期，最新的在前）
        all_files.sort(key=lambda x: x['name'], reverse=True)
        
        # 按类型筛选
        if file_type == 'image':
            filtered_files = [f for f in all_files if f['type'] == 'image']
        elif file_type == 'video':
            filtered_files = [f for f in all_files if f['type'] == 'video']
        else:
            filtered_files = all_files
        
        total = len(filtered_files)
        total_pages = (total + per_page - 1) // per_page
        
        # 分页
        start = (page - 1) * per_page
        end = start + per_page
        page_files = filtered_files[start:end]
        
        # 只为当前页的视频生成缩略图
        for file_info in page_files:
            if file_info['type'] == 'video':
                thumb_filename = os.path.splitext(file_info['name'])[0] + '.jpg'
                thumb_path = os.path.join(thumb_dir, thumb_filename)
                filepath = os.path.join(user_dir, file_info['name'])
                
                # 如果缩略图不存在则生成
                if not os.path.exists(thumb_path):
                    _generate_video_thumbnail(filepath, thumb_path)
                
                if os.path.exists(thumb_path):
                    file_info['thumb'] = f'/api/thumbnail/{user_id}/{thumb_filename}'
        
        return jsonify({
            'user_id': user_id,
            'files': page_files,
            'total': total,
            'images': image_count,
            'videos': video_count,
            'page': page,
            'per_page': per_page,
            'total_pages': total_pages
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/thumbnail/<user_id>/<filename>')
@login_required
def serve_thumbnail(user_id: str, filename: str):
    """提供缩略图访问"""
    try:
        from config import Config
        
        thumb_path = os.path.join(Config.BASE_DIR, 'downloads', '.thumbnails', user_id, filename)
        
        if not os.path.exists(thumb_path):
            return jsonify({'error': '缩略图不存在'}), 404
        
        return send_file(thumb_path, mimetype='image/jpeg')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@main_bp.route('/api/media-file/<user_id>/<filename>')
@login_required
def serve_media_file(user_id: str, filename: str):
    """提供媒体文件访问"""
    try:
        from config import Config
        
        current_user = get_current_user()
        account_user_id = current_user['id'] if current_user else None
        user_dir = resolve_media_dir(user_id, account_user_id)
        filepath = os.path.join(user_dir, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'error': '文件不存在'}), 404
        
        # conditional=True 支持 Range 请求，保证浏览器能对视频做 seek/加载元数据（pose预览首帧）
        return send_file(filepath, conditional=True)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ==================== 实时日志SSE接口 ====================

@main_bp.route('/api/logs/stream')
@admin_required
def stream_all_logs():
    """SSE: 实时推送所有日志"""
    def generate():
        q = log_manager.subscribe_all()
        try:
            # 先发送已有日志
            existing = log_manager.get_all_logs(limit=50)
            for log in reversed(existing):
                yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
            
            # 实时推送新日志
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield f"data: {json.dumps(entry.to_dict(), ensure_ascii=False)}\n\n"
                except queue.Empty:
                    # 发送心跳保持连接
                    yield f": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            log_manager.unsubscribe_all(q)
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@main_bp.route('/api/logs/stream/<task_id>')
@login_required
def stream_task_logs(task_id: str):
    """SSE: 实时推送指定任务日志"""
    def generate():
        q = log_manager.subscribe_task(task_id)
        try:
            # 先发送已有日志
            existing = log_manager.get_logs(task_id)
            for log in existing:
                yield f"data: {json.dumps(log, ensure_ascii=False)}\n\n"
            
            # 实时推送新日志
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield f"data: {json.dumps(entry.to_dict(), ensure_ascii=False)}\n\n"
                except queue.Empty:
                    yield f": heartbeat\n\n"
        except GeneratorExit:
            pass
        finally:
            log_manager.unsubscribe_task(task_id, q)
    
    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive'
        }
    )


@main_bp.route('/api/logs')
@admin_required
def get_all_logs_api():
    """获取所有日志（非SSE，用于初始化）"""
    limit = request.args.get('limit', 200, type=int)
    logs = log_manager.get_all_logs(limit=limit)
    return jsonify({'data': logs})


@main_bp.route('/api/logs/<task_id>')
@login_required
def get_task_logs_api(task_id: str):
    """获取指定任务日志"""
    limit = request.args.get('limit', 100, type=int)
    logs = log_manager.get_logs(task_id, limit=limit)
    return jsonify({'data': logs})


@main_bp.route('/api/download-queue', methods=['GET'])
@login_required
def get_download_queue():
    """获取当前下载队列：正在执行 + 排队等待"""
    return jsonify(download_service.queue_list())


@main_bp.route('/api/logs', methods=['DELETE'])
@admin_required
def clear_all_logs_api():
    """清除所有日志"""
    log_manager.clear_all_logs()
    return jsonify({'message': '日志已清除'})


# ============ 设备联动接口（安卓App推送链接/查询进度） ============

def device_token_required(f):
    """设备令牌认证：供安卓App等外部设备调用，令牌在网页配置页获取"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get('X-Device-Token', '')
        if not token:
            data = request.get_json(silent=True) or {}
            token = data.get('token', '') or ''
        if not token:
            token = request.args.get('token', '') or ''
        expected = database.get_device_token()
        if not expected or token != expected:
            return jsonify({'error': '设备令牌无效，请在网页配置页获取令牌'}), 401
        return f(*args, **kwargs)
    return wrapper


@main_bp.route('/api/device/token', methods=['GET'])
@admin_required
def get_device_token_api():
    """获取当前设备联动令牌（首次调用自动生成）"""
    return jsonify({'token': database.get_device_token()})


@main_bp.route('/api/device/token', methods=['POST'])
@admin_required
def rotate_device_token_api():
    """轮换设备联动令牌（旧令牌立即失效，手机App需重新填写）"""
    return jsonify({'token': database.rotate_device_token()})


@main_bp.route('/api/device/download', methods=['POST'])
@device_token_required
def device_download():
    """设备端提交分享链接入队下载"""
    data = request.get_json() or {}
    share_url = (data.get('url') or '').strip()
    if not share_url:
        return jsonify({'error': '请输入分享链接'}), 400

    share_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)/status/(\d+)', share_url)
    if share_re:
        screen_name = share_re.group(1)
        status_id = share_re.group(2)
    else:
        user_re = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)', share_url)
        if not user_re:
            return jsonify({'error': '无法识别的链接，请提供 twitter.com 或 x.com 的分享链接'}), 400
        screen_name = user_re.group(1)
        status_id = None

    force = data.get('force', False)
    task = download_service.create_task(
        screen_name, 'all', None,
        export_xlsx=False, create_zip=True,
        single_tweet=status_id, force=force, link=share_url
    )
    return jsonify({
        'task_id': task.task_id,
        'user_id': screen_name,
        'queue_size': download_service.queue_size(),
        'message': f'已加入下载队列（@{screen_name}）'
    })


@main_bp.route('/api/device/queue', methods=['GET'])
@device_token_required
def device_queue():
    """设备端查询下载队列与进度：正在执行 + 等待中 + 近期历史"""
    q = download_service.queue_list()
    return jsonify({
        'running': q.get('running', []),
        'waiting': q.get('waiting', []),
        'history': q.get('history', [])
    })