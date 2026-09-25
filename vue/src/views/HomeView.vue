<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Navbar from '../components/Navbar.vue'
import PageHeader from '../components/PageHeader.vue'
import i18n from '../utils/i18n'
import { getStatusText, formatElapsedTime } from '../utils/utils'
import { ElMessage } from 'element-plus'

const t = (key: string, params: Record<string, any> = {}) => i18n.t(key, params)

const userId = ref('')
const shareUrl = ref('')
const taskId = ref('')
const status = ref('pending')
const progress = ref(0)
const totalFiles = ref(0)
const downloadedFiles = ref(0)
const elapsedTime = ref('0' + t('time.seconds'))
const errorMessage = ref('')
const isDownloading = ref(false)
const queueCount = ref(0)
const force = ref(false)
const selective = ref(false)
const previewing = ref(false)
const previewUser = ref('')
const previewScreen = ref('')
const selectiveItems = ref<any[]>([])
const selectedIds = ref<Set<string>>(new Set())

let progressInterval: ReturnType<typeof setInterval> | null = null
let timeInterval: ReturnType<typeof setInterval> | null = null
let startTime: Date | null = null

const statusText = computed(() => getStatusText(status.value))

const updateElapsedTime = () => {
  if (!startTime) return
  const diff = Math.floor((new Date().getTime() - startTime.getTime()) / 1000)
  elapsedTime.value = formatElapsedTime(diff)
}

const stopTimers = () => {
  if (progressInterval) { clearInterval(progressInterval); progressInterval = null; }
  if (timeInterval) { clearInterval(timeInterval); timeInterval = null; }
}

const fetchProgress = async () => {
  if (!taskId.value) return
  try {
    const response = await fetch(`/api/progress/${taskId.value}`)
    const data = await response.json()
    if (response.ok) {
      status.value = data.status
      progress.value = Math.min(100, data.progress || 0)
      const dl = data.downloaded_files || 0
      downloadedFiles.value = dl
      totalFiles.value = Math.max(dl, data.total_files || 0)
      const skippedFiles = data.skipped_files || 0
      errorMessage.value = data.error_message || ''

      // 全部文件已存在：立即提示并结束任务
      if (totalFiles.value > 0 && downloadedFiles.value === 0 && skippedFiles === totalFiles.value) {
        isDownloading.value = false
        stopTimers()
        status.value = 'completed'
        ElMessage.info({
          message: `全部${totalFiles.value}个文件已存在，跳过下载`,
          duration: 2500,
          showClose: false
        })
      } else if (data.status === 'completed' || data.status === 'failed') {
        isDownloading.value = false
        stopTimers()
        if (skippedFiles > 0 && downloadedFiles.value === 0) {
          ElMessage.info({
            message: `下载完成，共跳过${skippedFiles}个已存在文件`,
            duration: 2500,
            showClose: false
          })
        } else if (downloadedFiles.value > 0) {
          ElMessage.success({
            message: `下载完成，新增${downloadedFiles.value}，跳过${skippedFiles}`,
            duration: 2500,
            showClose: false
          })
        }
      }
    }
  } catch (error) {
    console.error(t('home.requestFailed'), error)
  }
}

const startDownload = async () => {
  if (!userId.value.trim()) {
    ElMessage.warning(t('home.enterUserId'))
    return
  }
  isDownloading.value = true
  status.value = 'pending'
  progress.value = 0
  totalFiles.value = 0
  downloadedFiles.value = 0
  errorMessage.value = ''
  elapsedTime.value = '0' + t('time.seconds')
  startTime = new Date()

  try {
    const response = await fetch('/api/download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        user_id: userId.value.trim(),
        download_type: 'all',
        export_xlsx: true,
        force: force.value
      })
    })
    const data = await response.json()
    if (response.ok) {
      if (data.task_id) {
        taskId.value = data.task_id
        status.value = 'queued'
        // 入队成功即复位按钮，允许继续添加其他用户名/链接；进度可在历史页队列查看
        isDownloading.value = false
        if (progressInterval) stopTimers()
      } else if (data.tasks && data.tasks.length > 0) {
        ElMessage.success(data.message)
        isDownloading.value = false
        setTimeout(() => {
          window.location.href = '/history'
        }, 1500)
        return
      }
      ElMessage.success(data.message)
    } else {
      ElMessage.error(data.error || t('home.downloadFailed'))
      isDownloading.value = false
    }
  } catch (error) {
    ElMessage.error(t('home.requestFailed') + (error as Error).message)
    isDownloading.value = false
  }
}

const startShareDownload = async () => {
  if (!shareUrl.value.trim()) {
    ElMessage.warning(t('home.enterUserId'))
    return
  }
  // 支持持续添加链接：即使有任务在跑/排队也允许继续入队，新链接自动进入下载队列
  if (progressInterval) stopTimers()
  isDownloading.value = true
  status.value = 'queued'
  progress.value = 0
  totalFiles.value = 0
  downloadedFiles.value = 0
  errorMessage.value = ''
  elapsedTime.value = '0' + t('time.seconds')
  startTime = new Date()

  try {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 25000)
    const response = await fetch('/api/download-share', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url: shareUrl.value.trim(), force: force.value }),
      signal: controller.signal
    })
    clearTimeout(timer)
    const data = await response.json()
    if (response.ok && data.task_id) {
      taskId.value = data.task_id
      queueCount.value = data.queue_size || 0
      // 入队成功即复位按钮，可继续添加下一个链接；下载进度交由队列面板跟踪
      isDownloading.value = false
      if (queueCount.value > 1) {
        ElMessage.success({
          message: `已加入下载队列，前面还有 ${queueCount.value - 1} 个任务`,
          duration: 2500,
          showClose: false
        })
      } else {
        ElMessage.success({
          message: data.message || '已开始解析下载',
          duration: 2500,
          showClose: false
        })
      }
      progressInterval = setInterval(fetchProgress, 1000)
      timeInterval = setInterval(updateElapsedTime, 1000)
      shareUrl.value = '' // 清空输入框，便于继续添加下一个链接
    } else {
      ElMessage.error(data.error || t('home.downloadFailed'))
      isDownloading.value = false
    }
  } catch (error) {
    const errMsg = (error as Error)?.name === 'AbortError'
      ? '请求超时，请确认服务正常后重试'
      : (t('home.requestFailed') + (error as Error).message)
    ElMessage.error(errMsg)
    isDownloading.value = false
  }
}

const normalDownload = () => {
  selective.value = false
  force.value = false
  startShareDownload()
}

const forceDownload = () => {
  selective.value = false
  force.value = true
  startShareDownload()
}

const selectivDownload = () => {
  selective.value = true
  fetchPreview()
}

const downloadZip = async () => {
  if (!userId.value.trim()) return

  try {
    const response = await fetch(`/api/zip/${encodeURIComponent(userId.value.trim())}`, { method: 'POST' })
    const data = await response.json()

    if (response.ok) {
      if (data.download_url) {
        window.location.href = data.download_url
      }
    } else {
      ElMessage.error(data.error || t('home.zipFailed'))
    }
  } catch (error) {
    ElMessage.error(t('home.zipFailed'))
  }
}

const selectedCount = computed(() => selectedIds.value.size)
const allSelected = computed(() => selectiveItems.value.length > 0 && selectedIds.value.size === selectiveItems.value.length)

const toggleItem = (key: string) => {
  const next = new Set(selectedIds.value)
  if (next.has(key)) next.delete(key)
  else next.add(key)
  selectedIds.value = next
}

const toggleAll = () => {
  if (allSelected.value) selectedIds.value = new Set()
  else selectedIds.value = new Set(selectiveItems.value.map(it => it.key))
}

const fetchPreview = async () => {
  const url = shareUrl.value.trim()
  if (!url) {
    ElMessage.warning(t('home.enterUserId'))
    return
  }
  previewing.value = true
  try {
    const response = await fetch('/api/preview-media', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url })
    })
    const data = await response.json()
    if (response.ok) {
      previewUser.value = data.user_name || ''
      previewScreen.value = data.screen_name || ''
      selectiveItems.value = (data.items || []).map((it: any) => ({
        ...it,
        key: `${it.tweet_id}_${it.media_index}_${it.media_id || it.url}`
      }))
      selectedIds.value = new Set()
      if (!selectiveItems.value.length) {
        ElMessage.warning('该链接未抓取到可下载的媒体文件')
      } else {
        ElMessage.success(`已抓取 ${selectiveItems.value.length} 个文件，请勾选后下载`)
      }
    } else {
      ElMessage.error(data.error || '抓取媒体列表失败')
    }
  } catch (error) {
    ElMessage.error(t('home.requestFailed') + (error as Error).message)
  } finally {
    previewing.value = false
  }
}

const downloadSelected = async () => {
  const screen = previewScreen.value
  if (!screen) {
    ElMessage.warning('请先抓取媒体列表')
    return
  }
  if (selectedIds.value.size === 0) {
    ElMessage.warning('请至少选择一个文件')
    return
  }
  const items = selectiveItems.value.filter(it => selectedIds.value.has(it.key))
  isDownloading.value = true
  status.value = 'queued'
  progress.value = 0
  totalFiles.value = 0
  downloadedFiles.value = 0
  errorMessage.value = ''
  elapsedTime.value = '0' + t('time.seconds')
  startTime = new Date()

  try {
    const response = await fetch('/api/download-selected', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: screen,
        items: items.map(({ url, date_str, tweet_id, media_index, media_count, csv_info }) => ({
          url, date_str, tweet_id, media_index, media_count, csv_info
        })),
        force: force.value
      })
    })
    const data = await response.json()
    if (response.ok && data.task_id) {
      taskId.value = data.task_id
      queueCount.value = data.queue_size || 0
      ElMessage.success(data.message || `已入队下载 ${data.count || items.length} 个文件`)
      progressInterval = setInterval(fetchProgress, 1000)
      timeInterval = setInterval(updateElapsedTime, 1000)
    } else {
      ElMessage.error(data.error || t('home.downloadFailed'))
      isDownloading.value = false
    }
  } catch (error) {
    ElMessage.error(t('home.requestFailed') + (error as Error).message)
    isDownloading.value = false
  }
}

const onImgError = (e: Event) => {
  const el = e.target as HTMLElement
  el.style.display = 'none'
}

const mediaPreviewUrl = (it: any) => {
  if (it.type === 'image') return it.url
  return it.thumb_url || ''
}

const thumbUrl = (it: any) => {
  const src = mediaPreviewUrl(it)
  if (!src) return ''
  return `/api/thumb?url=${encodeURIComponent(src)}&screen=${encodeURIComponent(previewScreen.value)}`
}

const mediaLabel = (it: any) => {
  if (it.type === 'video') return '视频'
  return '图片'
}

onMounted(() => {
})
onUnmounted(() => {
  stopTimers()
})
</script>

<template>
  <Navbar active-page="home" />
  
  <div class="main-container" style="max-width: 800px;">
    <div class="banner-card">
      <div class="banner-content">
        <h1 class="banner-title">{{ t('home.title') }}</h1>
        <p class="banner-subtitle">{{ t('home.subtitle') }}</p>
      </div>
      <div class="banner-illustration">
        <svg viewBox="0 0 200 160" fill="none">
          <rect x="60" y="20" width="80" height="90" rx="8" stroke="#4A6CF7" stroke-width="2" fill="rgba(74,108,247,0.05)"/>
          <path d="M100 45v35m0 0l-12-12m12 12l12-12" stroke="#4A6CF7" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M75 95h50" stroke="#4A6CF7" stroke-width="2" stroke-linecap="round"/>
          <rect x="30" y="60" width="25" height="20" rx="3" stroke="#7B61FF" stroke-width="1.5" fill="rgba(123,97,255,0.05)"/>
          <circle cx="38" cy="68" r="3" stroke="#7B61FF" stroke-width="1.5"/>
          <path d="M32 76l6-4 4 3 6-5 5 4" stroke="#7B61FF" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          <rect x="145" y="55" width="25" height="25" rx="3" stroke="#00D4FF" stroke-width="1.5" fill="rgba(0,212,255,0.05)"/>
          <path d="M152 62l5 5m0-5l-5 5" stroke="#00D4FF" stroke-width="1.5" stroke-linecap="round"/>
          <path d="M150 74l3-2 2 1.5 3-2.5 3 2" stroke="#00D4FF" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
          <path d="M20 130c30-10 60 5 90-5s60 10 70 0" stroke="#4A6CF7" stroke-width="1" opacity="0.2"/>
        </svg>
      </div>
    </div>
    
    <div class="input-section">
      <div class="input-wrapper">
        <div class="input-field-wrapper">
          <div class="input-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2"/>
              <circle cx="12" cy="7" r="4"/>
            </svg>
            推特用户ID
          </div>
          <div class="input-row">
            <div class="input-icon-wrapper">
              <span class="input-icon">@</span>
              <input 
                type="text" 
                class="input-field" 
                v-model="userId" 
                :placeholder="t('home.userIdPlaceholder')"
                :disabled="isDownloading"
                @keyup.enter="startDownload"
              >
            </div>
            <button 
              class="download-btn"
              :class="{ loading: isDownloading }"
              :disabled="!userId.trim() || isDownloading"
              @click="startDownload"
            >
              <span v-if="isDownloading" class="btn-spinner"></span>
              <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
              </svg>
              {{ isDownloading ? t('home.downloading') : t('home.startDownload') }}
            </button>
          </div>
        </div>
      </div>
    </div>
    
    <div class="input-section">
      <div class="input-wrapper">
        <div class="input-field-wrapper">
          <div class="input-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/>
              <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>
            </svg>
            分享链接下载（支持 x.com / twitter.com 链接）
          </div>
          <div class="input-row">
            <div class="input-icon-wrapper">
              <input 
                type="text" 
                class="input-field" 
                v-model="shareUrl" 
                placeholder="例如: https://x.com/yykfPro/status/2097616305608393046?s=20"
                @keyup.enter="normalDownload()"
                :disabled="isDownloading || (selective && previewing)"
              >
            </div>
            <div class="download-actions">
              <button 
                class="download-btn"
                :class="{ loading: isDownloading }"
                :disabled="!shareUrl.trim() || isDownloading || (selective && previewing)"
                @click="normalDownload"
              >
                <span v-if="isDownloading" class="btn-spinner"></span>
                <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                  <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
                  <polyline points="7 10 12 15 17 10"/>
                  <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                下载
              </button>
              <button 
                class="sub-btn"
                :disabled="!shareUrl.trim() || isDownloading || (selective && previewing)"
                @click="forceDownload"
                title="忽略已存在文件，覆盖同名文件"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <path d="M21 12a9 9 0 11-2.64-6.36"/>
                  <polyline points="21 3 21 9 15 9"/>
                </svg>
                重新下载
              </button>
              <button 
                class="sub-btn"
                :disabled="!shareUrl.trim()"
                @click="selectivDownload"
                title="抓取推文下的文件，按需勾选下载"
              >
                <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                  <circle cx="11" cy="11" r="7"/>
                  <line x1="21" y1="21" x2="16.65" y2="16.65"/>
                </svg>
                选择下载
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 选择性下载面板 -->
    <div class="input-section" v-if="selective">
      <div class="selective-panel">
        <div class="input-label">选择性下载</div>

        <template v-if="selectiveItems.length">
          <div class="preview-toolbar">
            <div style="display:flex;align-items:center;gap:6px;font-size:13px;color:#555;">
              <span>{{ previewScreen ? '@' + previewScreen : '' }} · {{ selectiveItems.length }} 个文件</span>
            </div>
            <div style="display:flex;align-items:center;gap:12px;">
              <label style="display:flex;align-items:center;gap:5px;font-size:13px;color:#555;cursor:pointer;">
                <el-checkbox :model-value="allSelected" @change="toggleAll" size="small" />
                全选
              </label>
              <button class="select-download-btn" :disabled="!selectedCount" @click="downloadSelected">
                下载选中（{{ selectedCount }}）
              </button>
            </div>
          </div>

          <div class="preview-grid">
            <div
              v-for="it in selectiveItems"
              :key="it.key"
              class="preview-card"
              :class="{ 'is-checked': selectedIds.has(it.key) }"
              @click="toggleItem(it.key)"
            >
              <div class="preview-thumb">
                <svg v-if="it.type === 'video'" class="preview-cover-fallback" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
                  <rect x="2" y="2" width="20" height="20" rx="2.18"/><path d="M10 8l5 4-5 4V8z"/>
                </svg>
                <img
                  v-if="mediaPreviewUrl(it)"
                  class="preview-cover-img"
                  :src="thumbUrl(it)"
                  referrerpolicy="no-referrer"
                  loading="lazy"
                  alt="预览"
                  @error="onImgError"
                >
                <span class="preview-type" :class="it.type">{{ mediaLabel(it) }} {{ it.quality }}</span>
              </div>
              <div class="preview-check">
                <el-checkbox :model-value="selectedIds.has(it.key)" size="small" />
              </div>
            </div>
          </div>
        </template>

        <div v-else style="font-size:13px;color:#aaa;padding:10px 2px;">
          在上方「分享链接下载」输入链接，点击「选择下载」即可预览推文下的全部文件并勾选下载
        </div>
      </div>
    </div>

    <div v-if="taskId" class="progress-section">
      <div class="progress-header">
        <h3 class="progress-title">{{ t('home.downloadProgress') }}</h3>
        <span class="progress-status" :class="'status-' + status">{{ statusText }}</span>
      </div>
      <div class="progress-bar-wrapper">
        <div class="progress-bar-bg">
          <div class="progress-bar-fill" :style="{ width: progress + '%' }"></div>
        </div>
        <div class="progress-percentage">{{ progress }}%</div>
      </div>
      <div class="progress-info">
        <div class="progress-item" v-if="queueCount > 0">
          <div class="progress-item-label">队列等待</div>
          <div class="progress-item-value">{{ queueCount - 1 }}</div>
        </div>
        <div class="progress-item">
          <div class="progress-item-label">{{ t('home.downloaded') }}</div>
          <div class="progress-item-value">{{ downloadedFiles }}</div>
        </div>
        <div class="progress-item">
          <div class="progress-item-label">{{ t('home.totalFiles') }}</div>
          <div class="progress-item-value">{{ totalFiles }}</div>
        </div>
        <div class="progress-item">
          <div class="progress-item-label">{{ t('home.elapsedTime') }}</div>
          <div class="progress-item-value">{{ elapsedTime }}</div>
        </div>
      </div>
      <button v-if="status === 'completed'" class="download-complete-btn" @click="downloadZip">
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
          <polyline points="7 10 12 15 17 10"/>
          <line x1="12" y1="15" x2="12" y2="3"/>
        </svg>
        {{ t('home.downloadZip') }}
      </button>
      <div v-if="status === 'failed' && errorMessage" class="error-message">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="12" cy="12" r="10"/>
          <line x1="12" y1="8" x2="12" y2="12"/>
          <line x1="12" y1="16" x2="12.01" y2="16"/>
        </svg>
        {{ errorMessage }}
      </div>
    </div>
    
    <div class="guide-section">
      <h2 class="guide-title">
        <el-icon style="color: #4A6CF7;"><Warning /></el-icon>
        {{ t('home.guide') }}
      </h2>
      <div class="guide-steps">
        <div class="guide-step">
          <div class="step-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M16 12a4 4 0 10-8 0 4 4 0 008 0zm0 0v1.5a2.5 2.5 0 005 0V12a9 9 0 10-9 9m4.5-1.206a8.959 8.959 0 01-4.5 1.207"/>
            </svg>
          </div>
          <div class="step-content">
            <h4>{{ t('home.guideStep1') }}</h4>
            <p>{{ t('home.guideStep1Desc') }}</p>
          </div>
        </div>
        <div class="guide-step">
          <div class="step-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M12 2L2 7l10 5 10-5-10-5z"/>
              <path d="M2 17l10 5 10-5"/>
              <path d="M2 12l10 5 10-5"/>
            </svg>
          </div>
          <div class="step-content">
            <h4>{{ t('home.guideStep2') }}</h4>
            <p>{{ t('home.guideStep2Desc') }}</p>
          </div>
        </div>
        <div class="guide-step">
          <div class="step-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
            </svg>
          </div>
          <div class="step-content">
            <h4>{{ t('home.guideStep3') }}</h4>
            <p>{{ t('home.guideStep3Desc') }}</p>
          </div>
        </div>
        <div class="guide-step">
          <div class="step-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </div>
          <div class="step-content">
            <h4>{{ t('home.guideStep4') }}</h4>
            <p>{{ t('home.guideStep4Desc') }}</p>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.hint-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 15px;
  height: 15px;
  border-radius: 50%;
  background: #e5e7eb;
  color: #6b7280;
  font-size: 11px;
  font-weight: 600;
  cursor: help;
  user-select: none;
}
.hint-icon:hover {
  background: #d1d5db;
}
.download-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}
.download-actions .download-btn {
  width: auto;
  padding: 0 26px;
}
.sub-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 48px;
  padding: 0 18px;
  border: 1.5px solid rgba(229, 231, 235, 0.8);
  border-radius: 10px;
  background: rgba(255, 255, 255, 0.9);
  color: #4b5563;
  font-size: 14px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.2s ease;
  white-space: nowrap;
  flex-shrink: 0;
}
.sub-btn svg {
  flex-shrink: 0;
}
.sub-btn:hover:not(:disabled) {
  border-color: #4a6cf7;
  color: #4a6cf7;
  background: rgba(74, 108, 247, 0.05);
}
.sub-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.selective-panel {
  width: 100%;
}
.preview-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 8px 0 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid #f0f0f0;
}
.preview-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(120px, 1fr));
  gap: 12px;
  margin-top: 14px;
}
.preview-card {
  position: relative;
  border: 2px solid #e5e7eb;
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  background: #fafafa;
  transition: border-color 0.2s, box-shadow 0.2s;
  user-select: none;
}
.preview-card:hover {
  border-color: #c3cffe;
}
.preview-card.is-checked {
  border-color: #4A6CF7;
  box-shadow: 0 0 0 2px rgba(74,108,247,0.15);
}
.preview-thumb {
  position: relative;
  width: 100%;
  height: 110px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: linear-gradient(135deg, #f1f3fa, #e6ebff);
  overflow: hidden;
}
.preview-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.preview-cover-img {
  position: relative;
  z-index: 1;
}
.preview-cover-fallback {
  position: absolute;
  inset: 0;
  margin: auto;
  color: rgba(74, 108, 247, 0.75);
  pointer-events: none;
}
.preview-type {
  position: absolute;
  left: 6px;
  bottom: 6px;
  padding: 1px 7px;
  border-radius: 8px;
  font-size: 11px;
  color: #fff;
  background: rgba(0,0,0,0.55);
  backdrop-filter: blur(2px);
}
.preview-check {
  position: absolute;
  top: 6px;
  right: 6px;
  background: rgba(255,255,255,0.9);
  border-radius: 6px;
  padding: 1px;
}
.select-download-btn {
  border: none;
  background: #4A6CF7;
  color: #fff;
  font-size: 13px;
  padding: 7px 14px;
  border-radius: 8px;
  cursor: pointer;
  transition: background 0.2s;
}
.select-download-btn:hover:not(:disabled) {
  background: #3b5ae0;
}
.select-download-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
