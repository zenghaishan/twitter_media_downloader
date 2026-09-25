import os
import re
import time
import zipfile
import asyncio
import threading
import queue
from typing import Dict, Optional
from datetime import datetime

from config import Config
from models import DownloadTask
from downloader.twitter_downloader import TwitterDownloader
from logger import DownloadLogger
from realtime_logger import log_manager
import database
import webdav_sync


class DownloadService:
    """下载服务"""
    
    def __init__(self):
        self._tasks: Dict[str, DownloadTask] = {}
        # 必须用 RLock：_enqueue 在持锁状态下调用 _link_boost，后者再次获取 self._lock。
        # 若用 threading.Lock（不可重入），同一线程第二次获取会永久死锁，导致所有入队请求转圈。
        self._lock = threading.RLock()
        # 每个用户的串行锁，避免同一用户多个任务并发写同一目录导致 Windows WinError5
        self._user_locks: Dict[str, threading.Lock] = {}
        self._user_locks_guard = threading.Lock()
        
        # 全局下载队列：所有任务先入队，由单一工作线程串行消费，支持持续添加链接
        self._dl_queue = queue.Queue()
        self._task_order: list = []  # 入队顺序（排队等待的任务），供队列展示
        self._worker_started = False
        self._worker_thread = None
        self._worker_guard = threading.Lock()
        
        # 确保下载目录存在
        os.makedirs(Config.DOWNLOAD_FOLDER, exist_ok=True)
        
        # 确保日志目录存在
        os.makedirs(DownloadLogger.LOG_DIR, exist_ok=True)
        
        # 初始化数据库
        database.init_db()

        # 清理服务重启/异常退出后遗留的“排队中/下载中”僵尸任务，避免历史页出现永不结束的等待
        self._recover_stale_tasks()

        # 主动常驻启动队列消费线程：不依赖首次入队时才拉起，
        # 避免 worker 中途退出后入队任务因无人消费而永远停留在 queued。
        self._ensure_worker()

    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """获取任务（优先内存，其次数据库）"""
        # 先从内存获取
        task = self._tasks.get(task_id)
        if task:
            return task
        
        # 内存中没有，从数据库获取
        history = database.get_download_by_task_id(task_id)
        if history:
            task = DownloadTask(task_id, history['user_id'], account_user_id=history.get('account_user_id'))
            task.status = history['status']
            task.total_files = history.get('total_files', 0)
            task.downloaded_files = history.get('downloaded_files', 0)
            task.zip_path = history.get('zip_path')
            task.error_message = history.get('error_message')
            task.link = history.get('link')
            task.progress = 100 if history['status'] == 'completed' else 0
            return task
        
        return None
    
    def _recover_stale_tasks(self):
        """服务启动时把数据库中遗留的 queued/downloading 任务标记为 failed。
        这些任务可能因服务重启、debug reload、线程异常而未真正执行，若不清理
        会在历史页面永久显示为“排队中”。"""
        try:
            for row in database.get_stale_downloads():
                database.update_download_history(
                    row['task_id'],
                    status='failed',
                    error_message='服务重启导致任务中断，可点击“重新下载”恢复',
                    completed_at=time.strftime('%Y-%m-%d %H:%M:%S')
                )
        except Exception:
            import traceback
            traceback.print_exc()

    def create_task(self, user_id: str, download_type: str = 'all', account_user_id: int = None, export_xlsx: bool = False, create_zip: bool = True, single_tweet: str = None, force: bool = False, link: str = None) -> DownloadTask:
        """创建下载任务
        single_tweet: 若提供推文ID，则只下载该推文的媒体
        force: 若为True，忽略"已存在"去重，强制重新下载
        link: 原始提交链接（用于队列展示与重复提交排序提升）
        """
        # 重复链接去重：若该链接已有任务（等待中/下载中/历史记录），直接复用旧任务并提到队首，
        # 按最后一次添加时间排序，不新建重复记录。
        if link and not force:
            dup_task, reused = self._promote_existing_link(link)
            if reused:
                return dup_task

        # 检查是否已有该用户的下载记录（单推文分享任务不复用历史）
        existing_record = None if single_tweet else database.get_latest_download_by_user_id(user_id)
        
        if existing_record:
            # 复用之前的task_id
            task_id = existing_record['task_id']
            # 重置状态为downloading，同时更新created_at为当前时间
            database.update_download_history(
                task_id,
                status='downloading',
                downloaded_files=0,
                total_files=0,
                error_message=None,
                completed_at=None,
                created_at=time.strftime('%Y-%m-%d %H:%M:%S')
            )
        else:
            # 创建新的task_id（时间戳+随机后缀，彻底避免并发/同秒时UNIQUE冲突）
            import uuid
            task_id = f"{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
            # 添加到数据库
            database.add_download_history(task_id, user_id, account_user_id=account_user_id, link=link)
        
        task = DownloadTask(task_id, user_id, download_type, account_user_id=account_user_id, export_xlsx=export_xlsx, create_zip=create_zip)
        task.link = link
        if existing_record:
            database.update_download_history(task_id, link=link)
        
        with self._lock:
            self._tasks[task_id] = task
        
        # 标记为排队中并写入数据库
        task.status = 'queued'
        database.update_download_history(task_id, status='queued')
        
        # 记录实时日志
        log_manager.info(task_id, user_id, f'下载任务已加入队列: {user_id}', 'system')
        
        # 入队并由单一工作线程串行执行（而非每个任务开新线程）
        self._enqueue(task_id, link, single_tweet, force, None)
        
        return task

    def _promote_existing_link(self, link: str):
        """重复链接去重：命中时把已有任务提到队首并重新排队，不新建记录。
        优先级：等待中 > 下载中 > 历史记录（复用历史 task_id 强制重下）。
        返回 (task, True) 表示已复用（调用方直接返回该任务）；(None, False) 表示无重复。"""
        def _parse_tweet(link):
            m = re.search(r'(?:twitter\.com|x\.com)/[^/?]+/status/(\d+)', link)
            return m.group(1) if m else None

        # 1) 等待中的任务：直接提到队首，实现“按最后提交时间排序”
        with self._lock:
            for tid in list(self._task_order):
                t = self.get_task(tid)
                if t and t.status == 'queued' and getattr(t, 'link', '') == link:
                    self._task_order.remove(tid)
                    self._task_order.insert(0, tid)
                    return t, True
            # 2) 正在下载的任务：无法重排，提示已复用正在下载的任务
            for tid, t in list(self._tasks.items()):
                if t.status == 'downloading' and getattr(t, 'link', '') == link:
                    return t, True
            # 3) 历史记录：复用该 task_id 重新入队（强制重新下载）
            hist = database.get_last_download_by_link(link)
            if hist and hist.get('status') != 'downloading':
                tid = hist['task_id']
                t = DownloadTask(
                    tid, hist['user_id'],
                    account_user_id=hist.get('account_user_id'),
                    create_zip=True
                )
                t.link = hist.get('link') or link
                t.status = 'queued'
                database.update_download_history(
                    tid,
                    status='queued',
                    downloaded_files=0,
                    total_files=0,
                    error_message=None,
                    completed_at=None,
                    created_at=time.strftime('%Y-%m-%d %H:%M:%S')
                )
                self._tasks[tid] = t
                self._task_order.insert(0, tid)
                self._dl_queue.put((tid, _parse_tweet(link), True, None))  # force 重下
                self._ensure_worker()
                return t, True
        return None, False

    def create_selected_task(self, user_id: str, items: list, account_user_id: int = None, force: bool = False, link: str = None) -> DownloadTask:
        """创建“选择性下载”任务：仅下载用户勾选的媒体项。
        items: [{url, date_str, tweet_id, media_index, media_count, csv_info}]
        """
        import uuid
        task_id = f"sel_{user_id}_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        database.add_download_history(task_id, user_id, account_user_id=account_user_id, link=link)

        task = DownloadTask(task_id, user_id, 'all', account_user_id=account_user_id, export_xlsx=False, create_zip=True)
        # 记录待下载的媒体项，供 worker 消费
        task.selected_items = items
        task.link = link

        with self._lock:
            self._tasks[task_id] = task

        task.status = 'queued'
        database.update_download_history(task_id, status='queued')
        log_manager.info(task_id, user_id, f'选择性下载任务已加入队列: {user_id}（{len(items)} 个文件）', 'system')

        self._enqueue(task_id, link, None, force, items)

        return task

    def _enqueue(self, task_id: str, link: str, single_tweet, force, selected_items):
        """入队等待。若该链接曾经提交过（历史/队列中已有），则提到最前，提高排序"""
        with self._lock:
            boost = self._link_boost(task_id, link)
            if boost:
                self._task_order.insert(0, task_id)
                if link:
                    log_manager.info(task_id, '', f'已提交过的链接 @{link}，提高排序至队首', 'system')
            else:
                self._task_order.append(task_id)
        self._dl_queue.put((task_id, single_tweet, force, selected_items))
        self._ensure_worker()

    def _link_boost(self, task_id: str, link: str) -> bool:
        """判断该链接是否属于“曾经提交过”（队列中已有或历史已有其他任务），用于提高排序"""
        if not link:
            return False
        with self._lock:
            for tid, t in self._tasks.items():
                if tid != task_id and getattr(t, 'link', '') == link:
                    return True
            for tid in self._task_order:
                if tid == task_id:
                    continue
                t = self._tasks.get(tid)
                if t and getattr(t, 'link', '') == link:
                    return True
        hist = database.get_last_download_by_link(link)
        if hist and hist['task_id'] != task_id:
            return True
        return False

    def preview_media(self, user_id: str, single_tweet: str = None, account_user_id: int = None) -> dict:
        """只抓取媒体列表（不下载），供前端按需勾选。同步执行返回结果。"""
        proxy = Config.get_proxy(user_id=account_user_id)
        cookie = Config.get_cookie(user_id=account_user_id)

        downloader = TwitterDownloader(
            user_id=user_id,
            download_path=Config.DOWNLOAD_FOLDER,
            proxy=proxy,
            cookie=cookie,
            skip_existing=True,
            use_name_scoped_dir=True,
            tweet_id_hint=single_tweet
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(downloader.preview_media(target_tweet_id=single_tweet))
        except Exception:
            import traceback
            traceback.print_exc()
            raise
        finally:
            loop.close()
    
    def _ensure_worker(self):
        """确保唯一的队列消费工作线程存活。若已存在且仍在运行则复用，否则重启，
        避免 debug/reload 或线程异常退出后，入队任务因无人消费而永远停留在 queued。"""
        with self._worker_guard:
            w = self._worker_thread
            if w is not None and w.is_alive():
                self._worker_started = True
                return
            # worker 已不存在或已死亡，重建线程
            self._worker_thread = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker_started = True
            self._worker_thread.start()

    def _worker_loop(self):
        """串行消费全局下载队列"""
        while True:
            task_id, single_tweet, force, selected_items = self._dl_queue.get()
            # 出队后从等待列表中移除
            with self._lock:
                if task_id in self._task_order:
                    self._task_order.remove(task_id)
            try:
                task = self.get_task(task_id)
                if task is None:
                    continue
                self._run_download(task_id, single_tweet, force, selected_items)
            except Exception:
                import traceback
                traceback.print_exc()
            finally:
                self._dl_queue.task_done()

    def queue_size(self) -> int:
        """当前队列中等待执行的任务数（不含正在执行的任务）"""
        return self._dl_queue.qsize()

    def queue_list(self) -> dict:
        """返回下载队列当前状态：正在执行 + 等待中 + 近期历史（含原始链接），支撑全局弹窗队列"""
        waiting = []
        running = []
        with self._lock:
            order = list(self._task_order)
            for tid, t in list(self._tasks.items()):
                if t.status == 'downloading':
                    running.append({
                        'task_id': tid,
                        'user_id': t.user_id,
                        'link': getattr(t, 'link', '') or '',
                        'status': 'downloading',
                        'progress': t.progress,
                        'downloaded_files': t.downloaded_files,
                        'total_files': t.total_files,
                        'skipped_files': getattr(t, 'skipped_files', 0),
                        'failed_files': getattr(t, 'failed_files', 0)
                    })
        for tid in order:
            t = self.get_task(tid)
            if t:
                waiting.append({
                    'task_id': tid,
                    'user_id': t.user_id,
                    'link': getattr(t, 'link', '') or '',
                    'status': t.status or 'queued',
                    'progress': 0,
                    'downloaded_files': 0,
                    'total_files': 0
                })

        # 近期历史（已完成/失败），供“重新下载”按钮使用
        history = []
        rows = database.get_download_history(limit=30) or []
        for r in rows:
            st = r.get('status') or ''
            history.append({
                'task_id': r.get('task_id', ''),
                'user_id': r.get('user_id', ''),
                'link': r.get('link') or '',
                'status': st,
                'progress': 100 if st == 'completed' else 0,
                'downloaded_files': r.get('downloaded_files', 0),
                'total_files': r.get('total_files', 0)
            })
        return {'running': running, 'waiting': waiting, 'history': history}

    def re_download_task(self, task: DownloadTask, account_user_id: int = None) -> DownloadTask:
        """重新下载一个历史任务：复用原记录（不新增），按原链接强制重新入队"""
        task_id = task.task_id
        link = getattr(task, 'link', '') or ''
        single_tweet = None
        if link:
            m = re.search(r'(?:twitter\.com|x\.com)/([^/?]+)/status/(\d+)', link)
            single_tweet = m.group(2) if m else None
        # 直接复用当前 task_id，重置为该链接的下载任务并重新入队，绝不新增记录
        database.update_download_history(
            task_id,
            status='queued',
            downloaded_files=0,
            total_files=0,
            error_message=None,
            completed_at=None,
            created_at=time.strftime('%Y-%m-%d %H:%M:%S')
        )
        task.status = 'queued'
        task.downloaded_files = 0
        task.total_files = 0
        task.error_message = None
        task.link = link
        with self._lock:
            self._tasks[task_id] = task
        log_manager.info(task_id, task.user_id, f'重新下载已重新入队: {task.user_id}', 'system')
        self._enqueue(task_id, link, single_tweet, force=True, selected_items=None)
        return task

    def remove_task(self, task_id: str):
        """从内存中移除任务（删除历史记录时调用），避免删除后仍残留在队列面板中"""
        with self._lock:
            self._tasks.pop(task_id, None)
            if task_id in self._task_order:
                self._task_order.remove(task_id)

    def _run_download(self, task_id: str, single_tweet: str = None, force: bool = False, selected_items: list = None):
        """运行下载任务（在后台线程中执行）"""
        task = self.get_task(task_id)
        if not task:
            return
        
        # 初始化日志器
        file_logger = DownloadLogger(task_id, task.user_id)
        
        try:
            # 同一用户的任务串行执行，避免并发写同目录导致 WinError5
            with self._user_locks_guard:
                user_lock = self._user_locks.setdefault(task.user_id, threading.Lock())
            user_lock.acquire()
            try:
                self._run_download_locked(task_id, task, file_logger, single_tweet, force, selected_items)
            finally:
                user_lock.release()
        except Exception as e:
            import traceback
            traceback.print_exc()
            task.status = 'failed'
            task.error_message = str(e)
            task.end_time = time.time()
            
            log_manager.error(task_id, task.user_id, f'任务失败: {str(e)}', 'system')
            
            # 更新数据库
            database.update_download_history(
                task_id,
                status='failed',
                error_message=str(e),
                completed_at=time.strftime('%Y-%m-%d %H:%M:%S')
            )
        
        finally:
            # 清理内存中的任务（延迟清理，保持一段时间可查询）
            def cleanup():
                with self._lock:
                    if task_id in self._tasks:
                        del self._tasks[task_id]
            timer = threading.Timer(3600, cleanup)  # 1小时后清理
            timer.daemon = True
            timer.start()

    def _run_download_locked(self, task_id: str, task, file_logger, single_tweet: str = None, force: bool = False, selected_items: list = None):
        """实际执行下载（已持用户串行锁）"""
        try:
            task.status = 'downloading'
            database.update_download_history(task_id, status='downloading')
            log_manager.info(task_id, task.user_id, '开始下载...', 'system')
            
            # 创建用户下载目录（根目录取自定义配置，规则: 指定目录/昵称(用户id)/媒体）
            download_dir = database.get_config('download_dir', task.account_user_id)
            root_download_dir = download_dir.strip() if download_dir else Config.DOWNLOAD_FOLDER
            # 下载器内部会追加“昵称(用户id)”子目录，这里下载根目录即 download_dir
            user_download_path = root_download_dir
            os.makedirs(user_download_path, exist_ok=True)
            task.download_path = user_download_path
            log_manager.info(task_id, task.user_id, f'下载根目录: {root_download_dir}', 'system')
            
            # 进度回调函数
            def progress_callback(progress: int, downloaded_files: int, total_files: int, skipped_files: int = 0):
                task.progress = progress
                task.downloaded_files = downloaded_files
                task.total_files = total_files
                # 更新数据库
                database.update_download_history(
                    task_id,
                    downloaded_files=downloaded_files,
                    total_files=total_files
                )
            
            # 用户信息回调函数
            def user_info_callback(user_name: str, avatar_url: str):
                database.update_download_history(
                    task_id,
                    user_name=user_name,
                    avatar_url=avatar_url
                )
            
            # 从数据库获取配置（使用用户特定的配置）
            proxy = Config.get_proxy(user_id=task.account_user_id)
            cookie = Config.get_cookie(user_id=task.account_user_id)
            
            # 创建下载器
            downloader = TwitterDownloader(
                user_id=task.user_id,
                download_path=user_download_path,
                proxy=proxy,
                cookie=cookie,
                task_id=task_id,
                progress_callback=progress_callback,
                user_info_callback=user_info_callback,
                skip_existing=not force,  # 强制重新下载时忽略已存在文件
                max_retries=50,
                use_name_scoped_dir=True,
                tweet_id_hint=single_tweet
            )
            
            # 将实时日志管理器传递给下载器
            downloader.set_log_manager(log_manager)
            
            # 创建新的事件循环来运行异步下载
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                if selected_items:
                    result = loop.run_until_complete(downloader.download_selected(selected_items))
                elif single_tweet:
                    result = loop.run_until_complete(downloader.start_download(single_tweet_id=single_tweet))
                else:
                    result = loop.run_until_complete(downloader.start_download())
                # 媒体实际落在“昵称(用户id)”子目录，更新下载路径供ZIP打包使用
                task.download_path = downloader.user_info.get('save_path') or user_download_path
                downloaded = result.get('downloaded_files', 0)
                skipped = result.get('skipped_files', 0)
                failed = result.get('failed_files', 0)
                
                task.total_files = downloaded + skipped + failed
                task.downloaded_files = downloaded  # 仅统计新增下载数；跳过数在 skipped_files，便于前端识别"全部已存在"
                task.skipped_files = skipped
                task.failed_files = failed
                task.tweets_info = result.get('tweets_info', [])
                task.progress = 100
                
                log_manager.success(
                    task_id, task.user_id,
                    f'下载完成 - 新增: {downloaded}, 跳过: {skipped}, 失败: {failed}',
                    'system'
                )
                
                # 更新数据库
                database.update_download_history(
                    task_id,
                    user_name=result.get('user_name'),
                    avatar_url=result.get('avatar_url'),
                    total_files=task.total_files,
                    downloaded_files=task.downloaded_files
                )
            finally:
                loop.close()
            
            # 创建ZIP文件（如果启用）
            if task.create_zip:
                log_manager.info(task_id, task.user_id, '正在创建ZIP压缩文件...', 'system')
                self._create_zip(task)
                log_manager.success(task_id, task.user_id, f'ZIP文件创建完成: {os.path.basename(task.zip_path)}', 'system')
            else:
                log_manager.info(task_id, task.user_id, '跳过创建ZIP压缩包', 'system')
            
            task.status = 'completed'
            task.end_time = time.time()
            
            # 计算ZIP文件大小
            file_size = 0
            if task.zip_path and os.path.exists(task.zip_path):
                file_size = os.path.getsize(task.zip_path)
            
            # 更新数据库
            database.update_download_history(
                task_id,
                status='completed',
                zip_path=task.zip_path,
                file_size=file_size,
                completed_at=time.strftime('%Y-%m-%d %H:%M:%S')
            )
            
            log_manager.success(task_id, task.user_id, '任务已完成!', 'system')
            webdav_sync.push_async()
            
        except Exception as e:
            import traceback
            traceback.print_exc()
            task.status = 'failed'
            task.error_message = str(e)
            task.end_time = time.time()
            
            log_manager.error(task_id, task.user_id, f'任务失败: {str(e)}', 'system')
            
            # 更新数据库
            database.update_download_history(
                task_id,
                status='failed',
                error_message=str(e),
                completed_at=time.strftime('%Y-%m-%d %H:%M:%S')
            )
        
        finally:
            # 清理内存中的任务（延迟清理，保持一段时间可查询）
            def cleanup():
                with self._lock:
                    if task_id in self._tasks:
                        del self._tasks[task_id]
            timer = threading.Timer(3600, cleanup)  # 1小时后清理
            timer.daemon = True
            timer.start()
    
    def clear_all_cache(self) -> dict:
        """清理所有缓存文件和下载历史"""
        import shutil
        
        deleted_files = 0
        deleted_dirs = 0
        
        download_folder = Config.DOWNLOAD_FOLDER
        
        if os.path.exists(download_folder):
            try:
                # 遍历下载目录
                for item in os.listdir(download_folder):
                    item_path = os.path.join(download_folder, item)
                    
                    # 跳过隐藏文件
                    if item.startswith('.'):
                        continue
                    
                    if os.path.isfile(item_path):
                        # 删除文件（ZIP文件等）
                        os.remove(item_path)
                        deleted_files += 1
                    elif os.path.isdir(item_path):
                        # 删除用户下载目录
                        file_count = sum(len(files) for _, _, files in os.walk(item_path))
                        shutil.rmtree(item_path)
                        deleted_dirs += 1
                        deleted_files += file_count
            except Exception as e:
                raise Exception(f'清理缓存失败: {str(e)}')
        
        # 清空下载历史
        database.clear_all_download_history()

        # 同步清空内存任务/队列，避免清空后残留任务继续显示“排队中”或被误复用
        with self._lock:
            self._tasks.clear()
            self._task_order.clear()

        return {'deleted_files': deleted_files, 'deleted_dirs': deleted_dirs}
    
    def _create_zip(self, task: DownloadTask):
        """创建ZIP压缩文件，按文件类型分类"""
        # 文件类型分类
        video_exts = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.gif'}
        image_exts = {'.jpg', '.jpeg', '.png', '.webp', '.bmp'}
        
        def get_file_type(filename: str) -> str:
            """根据文件扩展名返回文件类型"""
            ext = os.path.splitext(filename)[1].lower()
            if ext in video_exts:
                return 'video'
            elif ext in image_exts:
                return 'image'
            else:
                return 'other'
        
        def get_category(filename: str) -> str:
            """根据文件扩展名返回分类文件夹名"""
            file_type = get_file_type(filename)
            if file_type == 'video':
                return 'videos'
            elif file_type == 'image':
                return 'images'
            else:
                return 'others'
        
        # 生成时间戳
        timestamp = time.strftime('%Y%m%d_%H%M%S')
        
        # 获取用户信息
        from database import get_download_by_task_id
        history = get_download_by_task_id(task.task_id)
        user_name = history.get('user_name') if history else None
        
        # 清理用户名中不安全的文件系统字符
        if user_name:
            user_name = re.sub(r'[/\\:*?"<>|]', '_', user_name).strip()
        
        # 构建名称：用户名_用户ID（如果有用户名）
        name_prefix = f'{user_name}_{task.user_id}' if user_name else task.user_id
        
        # 根据下载类型生成文件名
        download_type = task.download_type
        if download_type == 'video':
            zip_filename = f'{name_prefix}_video_{timestamp}.zip'
        elif download_type == 'image':
            zip_filename = f'{name_prefix}_img_{timestamp}.zip'
        else:  # all
            zip_filename = f'{name_prefix}_video_img_{timestamp}.zip'
        
        zip_path = os.path.join(Config.DOWNLOAD_FOLDER, zip_filename)
        
        # 构建根目录名称
        folder_name = name_prefix
        
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(task.download_path):
                for file in files:
                    file_type = get_file_type(file)
                    
                    # 根据下载类型过滤文件
                    if download_type == 'video' and file_type != 'video':
                        continue
                    if download_type == 'image' and file_type != 'image':
                        continue
                    
                    file_path = os.path.join(root, file)
                    # 获取分类文件夹
                    category = get_category(file)
                    # 构建ZIP内的路径：根目录/分类文件夹/原文件名
                    arcname = f'{folder_name}/{category}/{file}'
                    zipf.write(file_path, arcname)
            
            # 如果需要导出xlsx
            if task.export_xlsx and task.tweets_info:
                log_manager.info(task.task_id, task.user_id, '正在生成xlsx文件...', 'system')
                xlsx_path = self._create_xlsx(task)
                if xlsx_path:
                    xlsx_filename = f'{task.user_id}_tweets.xlsx'
                    zipf.write(xlsx_path, f'{folder_name}/{xlsx_filename}')
                    log_manager.success(task.task_id, task.user_id, f'xlsx文件生成完成，共 {len(task.tweets_info)} 条记录', 'system')
                    # 清理临时xlsx文件
                    try:
                        os.remove(xlsx_path)
                    except Exception:
                        pass
                else:
                    log_manager.error(task.task_id, task.user_id, 'xlsx文件生成失败', 'system')
        
        task.zip_path = zip_path
    
    def _create_xlsx(self, task: DownloadTask) -> str:
        """创建xlsx文件，返回文件路径"""
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
            
            wb = Workbook()
            ws = wb.active
            ws.title = '推文数据'
            
            # 定义表头
            headers = ['发布时间', '用户名', '用户ID', '推文内容', '媒体类型', '媒体URL', '下载URL']
            
            # 表头样式
            header_font = Font(bold=True, color='FFFFFF', size=11)
            header_fill = PatternFill(start_color='4A6CF7', end_color='4A6CF7', fill_type='solid')
            header_alignment = Alignment(horizontal='center', vertical='center')
            thin_border = Border(
                left=Side(style='thin'),
                right=Side(style='thin'),
                top=Side(style='thin'),
                bottom=Side(style='thin')
            )
            
            # 写入表头
            for col, header in enumerate(headers, 1):
                cell = ws.cell(row=1, column=col, value=header)
                cell.font = header_font
                cell.fill = header_fill
                cell.alignment = header_alignment
                cell.border = thin_border
            
            # 写入数据
            for row_idx, tweet in enumerate(task.tweets_info, 2):
                ws.cell(row=row_idx, column=1, value=tweet.get('time', '')).border = thin_border
                ws.cell(row=row_idx, column=2, value=tweet.get('name', '')).border = thin_border
                ws.cell(row=row_idx, column=3, value=tweet.get('screen_name', '')).border = thin_border
                
                # 推文内容，限制长度避免单元格过大
                text = tweet.get('text', '')
                if len(text) > 500:
                    text = text[:500] + '...'
                cell = ws.cell(row=row_idx, column=4, value=text)
                cell.border = thin_border
                cell.alignment = Alignment(wrap_text=True, vertical='top')
                
                ws.cell(row=row_idx, column=5, value=tweet.get('type', '')).border = thin_border
                ws.cell(row=row_idx, column=6, value=tweet.get('media_url', '')).border = thin_border
                ws.cell(row=row_idx, column=7, value=tweet.get('download_url', '')).border = thin_border
            
            # 设置列宽
            ws.column_dimensions['A'].width = 20
            ws.column_dimensions['B'].width = 15
            ws.column_dimensions['C'].width = 15
            ws.column_dimensions['D'].width = 60
            ws.column_dimensions['E'].width = 10
            ws.column_dimensions['F'].width = 40
            ws.column_dimensions['G'].width = 40
            
            # 保存到临时文件
            xlsx_path = os.path.join(Config.DOWNLOAD_FOLDER, f'{task.user_id}_{task.task_id}_tweets.xlsx')
            wb.save(xlsx_path)
            return xlsx_path
            
        except Exception as e:
            error_msg = f'创建xlsx失败: {e}'
            print(error_msg)
            log_manager.error(task.task_id, task.user_id, error_msg, 'system')
            return None


# 全局下载服务实例
download_service = DownloadService()
