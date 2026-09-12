import re
import time
import json
import os
import asyncio
import httpx
from datetime import datetime
from typing import Callable, Optional

from logger import DownloadLogger


class TwitterDownloader:
    def __init__(self, user_id: str, download_path: str, 
                 proxy: str = "",
                 cookie: str = "",
                 task_id: str = None,
                 progress_callback: Optional[Callable] = None,
                 user_info_callback: Optional[Callable] = None,
                 skip_existing: bool = True,
                 max_retries: int = 50,
                 use_name_scoped_dir: bool = False):
        self.user_id = user_id
        self.download_path = download_path
        self.proxy = proxy
        self.cookie = cookie
        self.task_id = task_id
        self.progress_callback = progress_callback
        self.user_info_callback = user_info_callback
        self.skip_existing = skip_existing
        self.max_retries = max_retries
        # 是否使用 “昵称(用户id)” 命名的子目录作为最终保存目录
        self.use_name_scoped_dir = use_name_scoped_dir
        
        # 初始化日志器
        self.logger = DownloadLogger(task_id or 'unknown', user_id) if task_id else None
        self._log_manager = None
        
        self.user_info = {
            'screen_name': user_id,
            'rest_id': None,
            'name': None,
            'avatar_url': None,
            'statuses_count': None,
            'media_count': None,
            'save_path': download_path,
            'cursor': None,
            'count': 0
        }
        
        self.headers = {
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36',
            'authorization': 'Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA',
            'cookie': self.cookie
        }
        
        # 从cookie中提取csrf token
        re_token = r'ct0=([a-f0-9]+)'
        match = re.search(re_token, self.cookie)
        if match:
            self.headers['x-csrf-token'] = match.group(1)
        
        self.headers['referer'] = f'https://twitter.com/{self.user_id}'
        
        self.request_count = 0
        self.tweets_info = []  # 收集帖子信息，用于导出xlsx
        self.down_count = 0
        self.total_files = 0
        self.downloaded_files = 0
        self.skipped_files = 0
        self.failed_files = 0
        
        # 下载配置
        self.has_retweet = False
        self.has_highlights = False
        self.has_likes = False
        self.has_video = True
        self.start_time_stamp = 655028357000  # 1990-10-04
        self.end_time_stamp = 2548484357000   # 2050-10-04
        self.start_label = True
        self.First_Page = True
        self.max_concurrent_requests = 8
        self.img_format = 'orig'
        self.orig_format = True
        
        # 处理代理配置
        self._proxy = proxy if proxy and proxy.strip() else None
    
    def set_log_manager(self, log_manager):
        """设置实时日志管理器"""
        self._log_manager = log_manager
    
    def _log(self, level: str, message: str, category: str = ''):
        """记录日志（同时写入文件和实时推送）"""
        # 写入文件日志
        if self.logger:
            getattr(self.logger, level, self.logger.info)(message)
        
        # 实时推送
        if self._log_manager and self.task_id:
            getattr(self._log_manager, level, self._log_manager.info)(
                self.task_id, self.user_id, message, category
            )
    
    def _get_client(self) -> httpx.AsyncClient:
        """创建httpx客户端，支持可选代理"""
        if self._proxy:
            return httpx.AsyncClient(proxy=self._proxy)
        return httpx.AsyncClient()
    
    def quote_url(self, url: str) -> str:
        return url.replace('{', '%7B').replace('}', '%7D')
    
    def stamp2time(self, msecs_stamp: int) -> str:
        timeArray = time.localtime(msecs_stamp / 1000)
        return time.strftime("%Y-%m-%d %H-%M", timeArray)
    
    def time2stamp(self, timestr: str) -> int:
        datetime_obj = datetime.strptime(timestr, "%Y-%m-%d")
        return int(time.mktime(datetime_obj.timetuple()) * 1000.0 + datetime_obj.microsecond / 1000.0)
    
    def time_comparison(self, now: int, start: int, end: int):
        start_label = True
        start_down = False
        if now >= start and now <= end:
            start_down = True
        elif now < start:
            start_label = False
        return [start_down, start_label]
    
    def _file_exists(self, file_path: str) -> bool:
        """检查文件是否已存在且大小大于0"""
        if not self.skip_existing:
            return False
        return os.path.exists(file_path) and os.path.getsize(file_path) > 0

    def _mkdir_retry(self, path: str, attempts: int = 5, delay: float = 0.8):
        """创建目录，遇 Windows 瞬时占用/权限抖动时重试"""
        for i in range(attempts):
            try:
                os.makedirs(path, exist_ok=True)
                if i > 0:
                    self._log('warning', f'目录创建重试成功({i}): {path}', 'system')
                return
            except PermissionError:
                self._log('warning', f'目录创建被拒绝({i+1}/{attempts}): {path!r}', 'system')
                if i == attempts - 1:
                    raise
                time.sleep(delay)
            except OSError:
                self._log('warning', f'目录创建OSError({i+1}/{attempts}): {path!r}', 'system')
                if i == attempts - 1:
                    raise
                time.sleep(delay)
    
    @staticmethod
    def _format_size(size: int) -> str:
        """格式化文件大小"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f'{size:.1f} {unit}'
            size /= 1024
        return f'{size:.1f} TB'
    
    async def get_other_info(self):
        url = f'https://twitter.com/i/api/graphql/xc8f1g7BYqr6VTzTbvNlGw/UserByScreenName?variables={{"screen_name":"{self.user_info["screen_name"]}","withSafetyModeUserFields":false}}&features={{"hidden_profile_likes_enabled":false,"hidden_profile_subscriptions_enabled":false,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"subscriptions_verification_info_verified_since_enabled":true,"highlights_tweets_tab_ui_enabled":true,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"responsive_web_graphql_timeline_navigation_enabled":true}}&fieldToggles={{"withAuxiliaryUserLabels":false}}'
        
        try:
            async with self._get_client() as client:
                response = await client.get(self.quote_url(url), headers=self.headers, timeout=30.0)
                self.request_count += 1
                
                if response.status_code != 200:
                    error_msg = f'HTTP {response.status_code}'
                    try:
                        error_data = response.json()
                        if 'errors' in error_data:
                            error_msg += f': {error_data["errors"][0].get("message", "")}'
                    except:
                        pass
                    raise Exception(error_msg)
                
                raw_data = response.json()
                
                # 检查是否有错误
                if 'errors' in raw_data:
                    error_msg = raw_data['errors'][0].get('message', 'Unknown error')
                    error_code = raw_data['errors'][0].get('code', '')
                    raise Exception(f'API Error {error_code}: {error_msg}')
                
                if 'data' not in raw_data:
                    raise Exception('响应中没有data字段')
                
                user_result = raw_data['data']['user']['result']
                
                self.user_info['rest_id'] = user_result.get('rest_id') or user_result.get('id')
                self.user_info['name'] = user_result.get('legacy', {}).get('name')
                # 将头像URL替换为高清版本 _400x400
                avatar_url = user_result.get('legacy', {}).get('profile_image_url_https')
                if avatar_url:
                    avatar_url = avatar_url.replace('_normal', '_400x400')
                self.user_info['avatar_url'] = avatar_url
                self.user_info['statuses_count'] = user_result.get('legacy', {}).get('statuses_count')
                self.user_info['media_count'] = user_result.get('legacy', {}).get('media_count')
                
                # 更新总文件数（估算）
                self.total_files = min(self.user_info['media_count'] or 100, 500)
                
                self._log('info', f'获取用户信息成功: {self.user_info["name"]} (@{self.user_info["screen_name"]})', 'system')
                self._log('info', f'媒体数量: {self.user_info["media_count"]}', 'system')
                
                # 调用用户信息回调函数
                if self.user_info_callback:
                    self.user_info_callback(self.user_info['name'], self.user_info['avatar_url'])
                
                return True
        except Exception as e:
            self._log('error', f'获取用户信息失败: {e}', 'system')
            print(f'获取用户信息失败: {e}')
            raise
    
    def get_heighest_video_quality(self, variants) -> str:
        if len(variants) == 1:
            return variants[0]['url']

        # 首选：带 bitrate 的 mp4 中选码率最高（m3u8 无 bitrate 字段会被自动跳过）
        max_bitrate = 0
        heighest_url = None
        for i in variants:
            if 'bitrate' in i:
                if int(i['bitrate']) > max_bitrate:
                    max_bitrate = int(i['bitrate'])
                    heighest_url = i['url']
        if heighest_url:
            return heighest_url

        # 兜底1：全部无 bitrate（如只有 m3u8 流）时，从 URL 路径解析 vid/{w}x{h}/ 选分辨率最高的 mp4
        def _res_height(url: str) -> int:
            m = re.search(r'/vid/(\d+)x(\d+)/', url)
            return int(m.group(2)) if m else 0

        max_h = 0
        fallback_url = None
        m3u8_url = None
        for i in variants:
            url = i.get('url', '')
            if not url:
                continue
            if i.get('content_type') == 'application/x-mpegURL':
                if not m3u8_url:
                    m3u8_url = url
                continue
            h = _res_height(url)
            if h > max_h:
                max_h = h
                fallback_url = url
        if fallback_url:
            return fallback_url

        # 兜底2：连分辨率都解析不到时退回 m3u8，最后退回第一个
        if m3u8_url:
            return m3u8_url
        return variants[0]['url'] if variants else None
    
    def get_url_from_content(self, content):
        photo_lst = []
        x_label = 'content' if (self.has_retweet or self.has_highlights) else 'item'
        
        for i in content:
            # 先检查是否有游标
            if 'cursor-bottom' in i.get('entryId', ''):
                self.user_info['cursor'] = i['content']['value']
            
            try:
                if 'promoted-tweet' in i['entryId']:
                    continue
                if 'tweet' in i['entryId']:
                    if 'tweet' in i[x_label]['itemContent']['tweet_results']['result']:
                        tweet_result = i[x_label]['itemContent']['tweet_results']['result']['tweet']
                        a = tweet_result['legacy']
                        tweet_msecs = int(tweet_result['edit_control']['editable_until_msecs']) - 3600000
                        tweet_id = tweet_result.get('rest_id', '')
                    else:
                        tweet_result = i[x_label]['itemContent']['tweet_results']['result']
                        a = tweet_result['legacy']
                        tweet_msecs = int(tweet_result['edit_control']['editable_until_msecs']) - 3600000
                        tweet_id = tweet_result.get('rest_id', '')
                    
                    timestr = self.stamp2time(tweet_msecs)
                    date_str = time.strftime("%Y-%m-%d", time.localtime(tweet_msecs / 1000))
                    result = self.time_comparison(tweet_msecs, self.start_time_stamp, self.end_time_stamp)
                    
                    if result[0]:
                        if 'retweeted_status_result' not in a:
                            name = self.user_info['name']
                            screen_name = self.user_info['screen_name']
                            if 'extended_entities' in a:
                                media_list = a['extended_entities']['media']
                                for idx, _media in enumerate(media_list):
                                    if 'video_info' in _media and self.has_video:
                                        url = self.get_heighest_video_quality(_media['video_info']['variants'])
                                        photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, name, f'@{screen_name}', _media['expanded_url'], 'Video', url, _media['media_url_https'], a['full_text']]))
                                        self.tweets_info.append({'time': timestr, 'name': name, 'screen_name': f'@{screen_name}', 'text': a['full_text'], 'type': 'Video', 'media_url': _media['expanded_url'], 'download_url': url})
                                    else:
                                        url = _media['media_url_https']
                                        photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, name, f'@{screen_name}', _media['expanded_url'], 'Image', url, '', a['full_text']]))
                                        self.tweets_info.append({'time': timestr, 'name': name, 'screen_name': f'@{screen_name}', 'text': a['full_text'], 'type': 'Image', 'media_url': _media['expanded_url'], 'download_url': url})
                        elif self.has_retweet:
                            name = a['retweeted_status_result']['result']['core']['user_results']['result']['legacy']['name']
                            screen_name = a['retweeted_status_result']['result']['core']['user_results']['result']['legacy']['screen_name']
                            full_text = a['retweeted_status_result']['result']['legacy']['full_text']
                            
                            if 'extended_entities' in a['retweeted_status_result']['result']['legacy'] and screen_name != self.user_info['screen_name']:
                                media_list = a['retweeted_status_result']['result']['legacy']['extended_entities']['media']
                                for idx, _media in enumerate(media_list):
                                    if 'video_info' in _media and self.has_video:
                                        url = self.get_heighest_video_quality(_media['video_info']['variants'])
                                        photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, name, f"@{screen_name}", _media['expanded_url'], 'Video', url, _media['media_url_https'], full_text]))
                                        self.tweets_info.append({'time': timestr, 'name': name, 'screen_name': f'@{screen_name}', 'text': full_text, 'type': 'Video', 'media_url': _media['expanded_url'], 'download_url': url})
                                    else:
                                        url = _media['media_url_https']
                                        photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, name, f"@{screen_name}", _media['expanded_url'], 'Image', url, '', full_text]))
                                        self.tweets_info.append({'time': timestr, 'name': name, 'screen_name': f'@{screen_name}', 'text': full_text, 'type': 'Image', 'media_url': _media['expanded_url'], 'download_url': url})
                    elif not result[1]:
                        self.start_label = False
                        break
                
                elif 'profile-conversation' in i['entryId']:
                    if 'tweet' in i[x_label]['items'][0]['item']['itemContent']['tweet_results']['result']:
                        tweet_result = i[x_label]['items'][0]['item']['itemContent']['tweet_results']['result']['tweet']
                        a = tweet_result['legacy']
                        tweet_msecs = int(tweet_result['edit_control']['editable_until_msecs']) - 3600000
                        tweet_id = tweet_result.get('rest_id', '')
                    else:
                        tweet_result = i[x_label]['items'][0]['item']['itemContent']['tweet_results']['result']
                        a = tweet_result['legacy']
                        tweet_msecs = int(tweet_result['edit_control']['editable_until_msecs']) - 3600000
                        tweet_id = tweet_result.get('rest_id', '')
                    
                    timestr = self.stamp2time(tweet_msecs)
                    date_str = time.strftime("%Y-%m-%d", time.localtime(tweet_msecs / 1000))
                    result = self.time_comparison(tweet_msecs, self.start_time_stamp, self.end_time_stamp)
                    
                    if result[0]:
                        if 'extended_entities' in a:
                            media_list = a['extended_entities']['media']
                            for idx, _media in enumerate(media_list):
                                if 'video_info' in _media and self.has_video:
                                    url = self.get_heighest_video_quality(_media['video_info']['variants'])
                                    photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, self.user_info['name'], f'@{self.user_info["screen_name"]}', _media['expanded_url'], 'Video', url, _media['media_url_https'], a['full_text']]))
                                    self.tweets_info.append({'time': timestr, 'name': self.user_info['name'], 'screen_name': f'@{self.user_info["screen_name"]}', 'text': a['full_text'], 'type': 'Video', 'media_url': _media['expanded_url'], 'download_url': url})
                                else:
                                    url = _media['media_url_https']
                                    photo_lst.append((url, date_str, tweet_id, idx, len(media_list), [tweet_msecs, self.user_info['name'], f'@{self.user_info["screen_name"]}', _media['expanded_url'], 'Image', url, '', a['full_text']]))
                                    self.tweets_info.append({'time': timestr, 'name': self.user_info['name'], 'screen_name': f'@{self.user_info["screen_name"]}', 'text': a['full_text'], 'type': 'Image', 'media_url': _media['expanded_url'], 'download_url': url})
                    elif not result[1]:
                        self.start_label = False
                        break
            
            except Exception as e:
                continue
        
        return photo_lst
    
    async def get_download_url(self, target_tweet_id: str = None):
        if self.has_highlights:
            url_top = f'https://twitter.com/i/api/graphql/w9-i9VNm_92GYFaiyGT1NA/UserHighlightsTweets?variables={{"userId":"{self.user_info["rest_id"]}","count":20,'
            url_bottom = '"includePromotedContent":true,"withVoice":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"c9s_tweet_anatomy_moderator_badge_enabled":true,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'
        elif self.has_likes:
            url_top = f'https://twitter.com/i/api/graphql/-fbTO1rKPa3nO6-XIRgEFQ/Likes?variables={{"userId":"{self.user_info["rest_id"]}","count":200,'
            url_bottom = '"includePromotedContent":false,"withClientEventToken":false,"withBirdwatchNotes":false,"withVoice":true,"withV2Timeline":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"c9s_tweet_anatomy_moderator_badge_enabled":true,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'
        elif self.has_retweet:
            url_top = f'https://twitter.com/i/api/graphql/2GIWTr7XwadIixZDtyXd4A/UserTweets?variables={{"userId":"{self.user_info["rest_id"]}","count":20,'
            url_bottom = '"includePromotedContent":false,"withQuickPromoteEligibilityTweetFields":true,"withVoice":true,"withV2Timeline":true}&features={"rweb_lists_timeline_redesign_enabled":true,"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}&fieldToggles={"withAuxiliaryUserLabels":false,"withArticleRichContentState":false}'
        else:
            url_top = f'https://twitter.com/i/api/graphql/Le6KlbilFmSu-5VltFND-Q/UserMedia?variables={{"userId":"{self.user_info["rest_id"]}","count":500,'
            url_bottom = '"includePromotedContent":false,"withClientEventToken":false,"withBirdwatchNotes":false,"withVoice":true,"withV2Timeline":true}&features={"responsive_web_graphql_exclude_directive_enabled":true,"verified_phone_label_enabled":false,"creator_subscriptions_tweet_preview_api_enabled":true,"responsive_web_graphql_timeline_navigation_enabled":true,"responsive_web_graphql_skip_user_profile_image_extensions_enabled":false,"tweetypie_unmention_optimization_enabled":true,"responsive_web_edit_tweet_api_enabled":true,"graphql_is_translatable_rweb_tweet_is_translatable_enabled":true,"view_counts_everywhere_api_enabled":true,"longform_notetweets_consumption_enabled":true,"responsive_web_twitter_article_tweet_consumption_enabled":false,"tweet_awards_web_tipping_enabled":false,"freedom_of_speech_not_reach_fetch_enabled":true,"standardized_nudges_misinfo":true,"tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled":true,"rweb_video_timestamps_enabled":true,"longform_notetweets_rich_text_read_enabled":true,"longform_notetweets_inline_media_enabled":true,"responsive_web_media_download_video_enabled":false,"responsive_web_enhance_cards_enabled":false}'
        
        if self.user_info['cursor']:
            url = url_top + f'"cursor":"{self.user_info["cursor"]}",' + url_bottom
        else:
            url = url_top + url_bottom
        
        try:
            async with self._get_client() as client:
                response = await client.get(self.quote_url(url), headers=self.headers, timeout=30.0)
                self.request_count += 1
                
                try:
                    raw_data = response.json()
                except Exception:
                    if 'Rate limit exceeded' in response.text:
                        if self.logger:
                            self.logger.error('API次数已超限')
                        print('API次数已超限')
                    else:
                        if self.logger:
                            self.logger.error('获取数据失败')
                        print('获取数据失败')
                    return None
                
                if self.has_highlights:
                    raw_data = raw_data['data']['user']['result']['timeline']['timeline']['instructions'][-1]['entries']
                elif self.has_retweet:
                    raw_data = raw_data['data']['user']['result']['timeline_v2']['timeline']['instructions'][-1]['entries']
                else:
                    raw_data = raw_data['data']['user']['result']['timeline_v2']['timeline']['instructions']
                
                if (self.has_retweet or self.has_highlights) and 'cursor-top' in raw_data[0]['entryId']:
                    return False
                
                if not self.has_retweet and not self.has_highlights:
                    for i in raw_data[-1]['entries']:
                        if 'bottom' in i['entryId']:
                            self.user_info['cursor'] = i['content']['value']
                
                if self.start_label:
                    if not self.has_retweet and not self.has_highlights:
                        if self.First_Page:
                            raw_data = raw_data[-1]['entries'][0]['content']['items']
                            self.First_Page = False
                        else:
                            if not raw_data or 'moduleItems' not in raw_data[0]:
                                # 没有更多数据了
                                return None
                            else:
                                raw_data = raw_data[0]['moduleItems']
                    
                    photo_lst = self.get_url_from_content(raw_data)
                else:
                    return None
                
                if not photo_lst:
                    photo_lst.append(True)
        
        except Exception as e:
            if self.logger:
                self.logger.error(f'获取推文信息错误: {e}')
            print(f'获取推文信息错误: {e}')
            return False
        
        return photo_lst
    
    @staticmethod
    def _parse_media_identity(url: str):
        """从媒体URL解析稳定的 媒体ID 与 清晰度。
        视频: .../vid/avc1/{w}x{h}/{hash}.mp4  → 媒体ID=文件哈希名, 清晰度=分辨率
        图片: .../media/{media_id}.{ext}
        同一媒体源在推文中多次出现时返回相同ID，作为目录内去重键。
        返回 (media_id_or_None, quality_or_None)
        """
        m = re.search(r'/vid/(?:avc1|hevc|[a-z0-9]+)/(\d+)x(\d+)/([A-Za-z0-9_-]+)\.mp4', url)
        if m:
            # 按实际清晰度标注：取短边作为p值（1200x540→540p，540x1200→540p）
            return m.group(3), f'{min(int(m.group(1)), int(m.group(2)))}p'
        m = re.search(r'/media/([A-Za-z0-9_-]+)\.', url)
        if m:
            return m.group(1), 'orig'
        return None, None

    async def download_file(self, url: str, date_str: str, tweet_id: str, media_index: int, media_count: int, csv_info: list, order: int):
        # 获取文件扩展名
        if '.mp4' in url:
            ext = 'mp4'
        else:
            try:
                if self.orig_format:
                    ext = csv_info[5][-3:]
                else:
                    ext = self.img_format
            except Exception as e:
                print(url)
                return False
        
        # 构建文件名：优先使用 媒体ID_清晰度（同一媒体在同用户目录内天然去重）；
        # 解析失败时回退到 日期_推文ID_序号
        media_id, quality = self._parse_media_identity(url)
        if media_id:
            file_name = f'{self.user_info["save_path"]}/{media_id}_{quality}.{ext}'
        else:
            if media_count == 1:
                # 单个文件：{date}_{tweet_id}.ext
                file_name = f'{self.user_info["save_path"]}/{date_str}_{tweet_id}.{ext}'
            else:
                # 多个文件：{date}_{tweet_id}_{index+1}.ext
                file_name = f'{self.user_info["save_path"]}/{date_str}_{tweet_id}_{media_index + 1}.{ext}'
        
        # 检查文件是否已存在
        if self._file_exists(file_name):
            self.skipped_files += 1
            self._log('info', f'[跳过] 文件已存在: {os.path.basename(file_name)}', 'download')
            # 更新进度
            if self.progress_callback:
                progress = min(int(((self.downloaded_files + self.skipped_files) / max(self.total_files, 1)) * 100), 100)
                self.progress_callback(progress, self.downloaded_files, self.total_files, self.skipped_files)
            return True
        
        # 处理URL
        if '.mp4' not in url and self.orig_format:
            url += '?name=orig'
        
        # 1) 下载媒体到内存（仅网络失败才重试）
        content = None
        effective_url = url
        count = 0
        while True:
            try:
                async with self._get_client() as client:
                    response = await client.get(self.quote_url(effective_url), timeout=(3.05, 16))
                    if response.status_code == 404:
                        raise Exception('404')
                    content = response.content
                    break
            except Exception as e:
                if '.mp4' in effective_url or self.orig_format or str(e) != "404":
                    count += 1
                    if count >= self.max_retries:
                        self.failed_files += 1
                        self._log('error', f'[下载失败] {os.path.basename(file_name)} - 错误: {e} (已重试{count}次)', 'download')
                        return False
                    self._log('warning', f'[重试] {os.path.basename(file_name)} - 第{count}/{self.max_retries}次重试', 'download')
                else:
                    effective_url = effective_url.replace('name=orig', 'name=4096x4096')
        
        # 2) 写盘（本地磁盘失败直接记为失败，不触发网络重试，避免计数虚增与刷屏）
        try:
            with open(file_name, 'wb') as f:
                f.write(content)
        except Exception as e:
            self.failed_files += 1
            self._log('error', f'[写入失败] {os.path.basename(file_name)} - 错误: {e}', 'download')
            return False
        
        # 3) 真正落盘成功后才累计下载数
        self.down_count += 1
        self.downloaded_files += 1
        file_size = len(content)
        size_str = self._format_size(file_size)
        self._log('success', f'[下载成功] {os.path.basename(file_name)} ({size_str})', 'download')
        
        # 更新进度
        if self.progress_callback:
            progress = min(int(((self.downloaded_files + self.skipped_files) / max(self.total_files, 1)) * 100), 100)
            self.progress_callback(progress, self.downloaded_files, self.total_files, self.skipped_files)
        
        return True
    
    async def download_control(self, target_tweet_id: str = None):
        page_count = 0
        while True:
            photo_lst = await self.get_download_url(target_tweet_id)
            
            # 没有更多数据，或 API 异常(返回False)，终止翻页
            if photo_lst is None or photo_lst is False:
                break
            
            # 空列表或只有True标记，继续下一页
            if not photo_lst or (len(photo_lst) == 1 and photo_lst[0] == True):
                continue
            
            page_count += 1
            self._log('info', f'开始处理第 {page_count} 页，包含 {len(photo_lst)} 个媒体文件', 'system')
            
            # 若指定了目标推文，只保留该推文的媒体；本页没有则继续翻页
            if target_tweet_id is not None:
                photo_lst = [it for it in photo_lst if str(it[2]) == str(target_tweet_id)]
                if not photo_lst:
                    continue
            
            tasks = []
            for order, item in enumerate(photo_lst):
                # item = (url, date_str, tweet_id, media_index, media_count, csv_info)
                task = self.download_file(item[0], item[1], item[2], item[3], item[4], item[5], order)
                tasks.append(task)
            
            await asyncio.gather(*tasks)
            self.user_info['count'] += len(photo_lst)
            
            # 记录进度
            if self.logger:
                self.logger.log_progress(self.downloaded_files, self.downloaded_files + self.skipped_files + self.failed_files, self.skipped_files)
            
            # 单推文场景：目标推文已下载完毕，立即终止翻页，避免重复下载与计数虚增
            if target_tweet_id is not None:
                break
    
    async def preview_media(self, target_tweet_id: str = None):
        """只抓取并返回媒体列表（不下载）。返回值供前端选择后按需下载。"""
        items = []
        if not await self.get_other_info():
            raise Exception('获取用户信息失败')
        page_count = 0
        while True:
            photo_lst = await self.get_download_url(target_tweet_id)
            if photo_lst is None or photo_lst is False:
                break
            if not photo_lst or (len(photo_lst) == 1 and photo_lst[0] == True):
                continue
            page_count += 1
            if target_tweet_id is not None:
                photo_lst = [it for it in photo_lst if str(it[2]) == str(target_tweet_id)]
                if not photo_lst:
                    continue
            for item in photo_lst:
                url, date_str, tid, midx, mcnt, csv = item[0], item[1], item[2], item[3], item[4], item[5]
                media_id, quality = self._parse_media_identity(url)
                ext = ('mp4' if '.mp4' in url else (csv[5][-3:] if csv and len(csv) > 5 else ''))
                thumb_url = url
                if '.mp4' in url:
                    thumb_url = csv[6] if csv and len(csv) > 6 and csv[6] else None
                items.append({
                    'url': url,
                    'date_str': date_str,
                    'tweet_id': str(tid),
                    'media_index': midx,
                    'media_count': mcnt,
                    'csv_info': csv,
                    'media_id': media_id or '',
                    'quality': quality or '',
                    'type': 'video' if '.mp4' in url else 'image',
                    'thumb_url': thumb_url
                })
            if target_tweet_id is not None:
                break
        return {
            'user_name': self.user_info.get('name'),
            'screen_name': self.user_info.get('screen_name', self.user_id),
            'items': items
        }

    async def download_selected(self, items):
        """按选中的媒体项（列表）下载"""
        start_time = time.time()
        os.makedirs(self.download_path, exist_ok=True)
        if not await self.get_other_info():
            raise Exception('获取用户信息失败')
        if self.use_name_scoped_dir and self.user_info.get('name'):
            display_name = re.sub(r'[/\\:*?"<>|]', '_', str(self.user_info['name'])).strip().rstrip('. ')
            folder = f'{display_name}({self.user_info["screen_name"]})'
            self.user_info['save_path'] = os.path.join(self.download_path, folder)
            self._mkdir_retry(self.download_path)
            self._mkdir_retry(self.user_info['save_path'])
            self._log('info', f'保存目录: {self.user_info["save_path"]}', 'system')

        download_tasks = []
        for order, item in enumerate(items):
            url = item.get('url')
            if not url:
                continue
            csv_info = item.get('csv_info') or [None, '', '', '', None, url, '', '']
            download_tasks.append(self.download_file(
                url,
                str(item.get('date_str', '')),
                str(item.get('tweet_id', '')),
                int(item.get('media_index', 0) or 0),
                int(item.get('media_count', 1) or 1),
                csv_info,
                order
            ))
        await asyncio.gather(*download_tasks)
        total_time = time.time() - start_time
        self._log('info', f'下载完成 - 成功: {self.downloaded_files}, 失败: {self.failed_files}, 跳过: {self.skipped_files}, 耗时: {total_time:.1f}秒', 'system')
        return {
            'user_name': self.user_info['name'],
            'avatar_url': self.user_info['avatar_url'],
            'media_count': self.user_info['media_count'],
            'downloaded_files': self.downloaded_files,
            'skipped_files': self.skipped_files,
            'failed_files': self.failed_files,
            'request_count': self.request_count,
            'total_time': total_time,
            'tweets_info': self.tweets_info
        }

    async def start_download(self, single_tweet_id: str = None):
        """开始下载任务
        single_tweet_id: 若提供，仅下载该推文ID下的所有媒体
        """
        start_time = time.time()
        
        # 创建下载根目录
        os.makedirs(self.download_path, exist_ok=True)
        
        self._log('info', f'下载根目录: {self.download_path}', 'system')
        
        # 获取用户信息
        if not await self.get_other_info():
            raise Exception('获取用户信息失败')
        
        # 若开启了“昵称(用户id)”子目录规则，在拿到昵称后确定最终保存目录
        if self.use_name_scoped_dir and self.user_info.get('name'):
            display_name = re.sub(r'[/\\:*?"<>|]', '_', str(self.user_info['name'])).strip().rstrip('. ')
            folder = f'{display_name}({self.user_info["screen_name"]})'
            self.user_info['save_path'] = os.path.join(self.download_path, folder)
            # Windows 下目录可能被瞬时占用/杀软锁定，做一次带重试的创建
            self._mkdir_retry(self.download_path)
            self._mkdir_retry(self.user_info['save_path'])
            self._log('info', f'保存目录: {self.user_info["save_path"]}', 'system')
        
        # 开始下载
        await self.download_control(target_tweet_id=single_tweet_id)
        
        # 计算总耗时
        total_time = time.time() - start_time
        
        # 记录任务完成
        self._log('info', f'下载完成 - 成功: {self.downloaded_files}, 失败: {self.failed_files}, 跳过: {self.skipped_files}, 耗时: {total_time:.1f}秒', 'system')
        
        return {
            'user_name': self.user_info['name'],
            'avatar_url': self.user_info['avatar_url'],
            'media_count': self.user_info['media_count'],
            'downloaded_files': self.downloaded_files,
            'skipped_files': self.skipped_files,
            'failed_files': self.failed_files,
            'request_count': self.request_count,
            'total_time': total_time,
            'tweets_info': self.tweets_info
        }
