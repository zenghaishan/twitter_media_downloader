<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import Navbar from '../components/Navbar.vue'
import PageHeader from '../components/PageHeader.vue'
import LoadingState from '../components/LoadingState.vue'
import EmptyState from '../components/EmptyState.vue'
import Pagination from '../components/Pagination.vue'
import i18n from '../utils/i18n'
import { getStatusText, formatFileSize, debounce } from '../utils/utils'
import { ElMessage, ElMessageBox } from 'element-plus'

const t = (key: string, params: Record<string, any> = {}) => i18n.t(key, params)

const user = ref<any>(null)
const history = ref<any[]>([])
const loading = ref(false)
const currentPage = ref(1)
const pageSize = ref(15)
const total = ref(0)
const zippingUsers = ref<string[]>([])

const filters = ref({
  keyword: '',
  status: '',
  date: ''
})

const totalPages = computed(() => Math.ceil(total.value / pageSize.value))

/* ===== 下载管理：实时队列（进行中 / 排队） ===== */
const queueRunning = ref<any[]>([])
const queueWaiting = ref<any[]>([])
const redoLoading = ref(false)
let queueInterval: ReturnType<typeof setInterval> | null = null

const hasActiveQueue = computed(() => queueRunning.value.length + queueWaiting.value.length > 0)

const qsText = (s: string) => {
  switch (s) {
    case 'downloading': return '下载中'
    case 'queued': return '等待中'
    case 'completed': return '已完成'
    case 'failed': return '失败'
    default: return getStatusText(s)
  }
}
const qsClass = (s: string) => {
  switch (s) {
    case 'downloading': return 'q-status-downloading'
    case 'queued': return 'q-status-queued'
    case 'completed': return 'q-status-completed'
    case 'failed': return 'q-status-failed'
    default: return ''
  }
}
const linkLabel = (item: any) => {
  const link = item.link || ''
  if (link) {
    try { return link.replace(/^https?:\/\/(x|twitter)\.com\//, '') } catch (e) { return link }
  }
  return item.user_id ? '@' + item.user_id : ''
}
const displayTotal = (item: any) => Math.max(item.downloaded_files || 0, item.total_files || 0)
const rowProgress = (item: any) => {
  if ((item.status === 'downloading' || item.status === 'queued') && item.total_files) {
    const denom = displayTotal(item) || 1
    return Math.min(100, Math.round((item.downloaded_files / denom) * 100))
  }
  return item.status === 'completed' ? 100 : 0
}
const fetchQueue = async () => {
  try {
    const response = await fetch('/api/download-queue')
    if (response.ok) {
      const data = await response.json()
      queueRunning.value = data.running || []
      queueWaiting.value = data.waiting || []
    }
  } catch (e) {
    // 忽略网络错误，保持安静轮询
  }
}
const redownload = async (item: any) => {
  if (redoLoading.value) return
  redoLoading.value = true
  try {
    const response = await fetch('/api/re-download', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: item.task_id })
    })
    const data = await response.json()
    if (response.ok && data.task_id) {
      ElMessage.success(data.message || '已重新入队下载')
      fetchQueue()
      fetchHistory(currentPage.value, false)
    } else {
      ElMessage.error(data.error || '重新下载失败')
    }
  } catch (error) {
    ElMessage.error(t('home.requestFailed') + (error as Error).message)
  } finally {
    redoLoading.value = false
  }
}
/* ===== 下载管理：实时队列 ===== */

const debounceSearch = debounce(() => {
  fetchHistory(1)
})

const resetFilters = () => {
  filters.value = {
    keyword: '',
    status: '',
    date: ''
  }
  fetchHistory(1)
}

const fetchHistory = async (page = 1, showLoading = true) => {
  if (showLoading) {
    loading.value = true
  }
  try {
    const params = new URLSearchParams({
      page: page.toString(),
      per_page: pageSize.value.toString()
    })
    if (filters.value.keyword) {
      params.append('keyword', filters.value.keyword)
    }
    if (filters.value.status) {
      params.append('status', filters.value.status)
    }
    if (filters.value.date) {
      params.append('date', filters.value.date)
    }

    const response = await fetch(`/api/history?${params.toString()}`)
    const data = await response.json()
    const newData = data.data || []

    if (JSON.stringify(newData) !== JSON.stringify(history.value)) {
      history.value = newData
    }
    total.value = data.total || 0
    currentPage.value = data.page || 1

    fetchMissingAvatars(newData)
  } catch (error) {
    if (showLoading) {
      ElMessage.error(t('history.fetchFailed'))
    }
    history.value = []
  } finally {
    if (showLoading) {
      loading.value = false
    }
  }
}

const fetchMissingAvatars = async (items: any[]) => {
  const missingUsers = new Set<string>()
  for (const item of items) {
    // 没有头像，或头像为 Twitter 外链（浏览器无法直接访问，用本地化头像兜底）都纳入拉取
    if (item.user_id && (!item.avatar_url || String(item.avatar_url).startsWith('http'))) {
      missingUsers.add(item.user_id)
    }
  }

  for (const userId of missingUsers) {
    try {
      const resp = await fetch(`/api/avatar/${encodeURIComponent(userId)}`)
      const data = await resp.json()
      if (data.avatar_url) {
        for (const item of history.value) {
          if (item.user_id === userId) {
            item.avatar_url = data.avatar_url
          }
        }
      }
    } catch (e) {
      // 静默失败
    }
  }
}

const downloadFile = (taskId: string) => {
  window.location.href = `/api/download/${encodeURIComponent(taskId)}`
}

const deleteHistory = async (taskId: string) => {
  try {
    await ElMessageBox.confirm(
      t('history.deleteConfirm'),
      t('history.deleteTitle'),
      {
        confirmButtonText: t('common.confirm'),
        cancelButtonText: t('common.cancel'),
        type: 'warning'
      }
    )

    const response = await fetch(`/api/history/${encodeURIComponent(taskId)}`, { method: 'DELETE' })
    if (response.ok) {
      ElMessage.success(t('history.deleted'))
      fetchHistory(currentPage.value)
    } else {
      ElMessage.error(t('history.deleteFailed'))
    }
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(t('history.deleteFailed'))
    }
  }
}

// 生成UUID
const generateUUID = () => {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = Math.random() * 16 | 0
    const v = c === 'x' ? r : (r & 0x3 | 0x8)
    return v.toString(16)
  })
}

// 获取或生成用户UUID
const getUserUUID = (userId: string) => {
  const storageKey = `zip_uuid_${userId}`
  let uuid = localStorage.getItem(storageKey)
  if (!uuid) {
    uuid = generateUUID()
    localStorage.setItem(storageKey, uuid)
  }
  return uuid
}

const createZip = async (userId: string) => {
  if (zippingUsers.value.includes(userId)) return

  zippingUsers.value.push(userId)

  try {
    const uuid = getUserUUID(userId)
    const response = await fetch(`/api/zip/${encodeURIComponent(userId)}?uuid=${uuid}`, { method: 'POST' })
    const data = await response.json()

    if (response.ok) {
      ElMessage.success(t('history.zipCreated'))
      if (data.download_url) {
        window.location.href = data.download_url
      }
      fetchHistory(currentPage.value)
    } else {
      ElMessage.error(data.error || t('history.zipFailed'))
    }
  } catch (error) {
    ElMessage.error(t('history.zipFailed'))
  } finally {
    zippingUsers.value = zippingUsers.value.filter(u => u !== userId)
  }
}

const clearCache = async () => {
  try {
    await ElMessageBox.confirm(
      t('history.clearCacheConfirm'),
      t('history.clearCacheTitle'),
      {
        confirmButtonText: t('history.clearCacheButton'),
        cancelButtonText: t('common.cancel'),
        type: 'warning'
      }
    )

    loading.value = true
    const response = await fetch('/api/cache', { method: 'DELETE' })
    const data = await response.json()

    if (response.ok) {
      ElMessage.success(data.message)
      fetchHistory(currentPage.value)
    } else {
      ElMessage.error(data.error || t('history.clearFailed'))
    }
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error(t('history.clearFailed'))
    }
  } finally {
    loading.value = false
  }
}

let refreshInterval: ReturnType<typeof setInterval> | null = null

const fetchUser = async () => {
  try {
    const response = await fetch('/api/auth/me')
    if (response.ok) {
      user.value = await response.json()
    }
  } catch (error) {
    // 静默失败
  }
}

onMounted(() => {
  fetchUser()
  fetchHistory()
  fetchQueue()
  refreshInterval = setInterval(() => {
    fetchHistory(currentPage.value, false)
    fetchQueue()
  }, 3000)
  queueInterval = setInterval(fetchQueue, 3000)
})

onUnmounted(() => {
  if (refreshInterval) {
    clearInterval(refreshInterval)
    refreshInterval = null
  }
  if (queueInterval) {
    clearInterval(queueInterval)
    queueInterval = null
  }
})
</script>

<template>
  <Navbar active-page="history" />
  
  <div class="main-container">
    <PageHeader 
      :title="t('history.title')" 
      :subtitle="t('history.subtitle')"
      icon="<circle cx='12' cy='12' r='10'/><polyline points='12 6 12 12 16 14'/>"
    >
      <button v-if="user && user.role === 'admin'" class="btn btn-secondary" @click="clearCache">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <path d="M21 12a9 9 0 11-18 0 9 9 0 0118 0z"/>
          <path d="M9 10h6M9 14h6"/>
        </svg>
        {{ t('history.clearCache') }}
      </button>
    </PageHeader>
    
    <!-- 下载管理：实时队列（进行中 / 排队） -->
    <div v-if="hasActiveQueue" class="history-card queue-panel">
      <div class="queue-panel-head">
        <span class="queue-panel-title">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
            <rect x="3" y="3" width="7" height="7" rx="1.5"/>
            <rect x="14" y="3" width="7" height="7" rx="1.5"/>
            <rect x="3" y="14" width="7" height="7" rx="1.5"/>
            <rect x="14" y="14" width="7" height="7" rx="1.5"/>
          </svg>
          下载管理 · 进行中和排队
        </span>
        <span class="queue-panel-badge">{{ queueRunning.length + queueWaiting.length }} 个任务</span>
      </div>
      <div style="margin-top: 6px;">
        <div v-for="item in queueRunning" :key="item.task_id" class="queue-row">
          <div class="queue-row-main">
            <div class="queue-row-top">
              <span class="queue-status" :class="qsClass(item.status)">{{ qsText(item.status) }}</span>
              <span class="queue-link" :title="item.link">{{ linkLabel(item) }}</span>
              <button class="queue-redo" title="重新下载" :disabled="redoLoading" @click="redownload(item)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
                  <polyline points="23 4 23 10 17 10"/>
                  <path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/>
                </svg>
              </button>
            </div>
            <div class="queue-progress-line">
              <div class="queue-progress-fill" :style="{ width: rowProgress(item) + '%' }"></div>
            </div>
            <div class="queue-row-foot">
              <span class="queue-files">{{ item.downloaded_files || 0 }}/{{ displayTotal(item) }} 个文件</span>
              <span class="queue-pct">{{ rowProgress(item) }}%</span>
            </div>
          </div>
        </div>
        <div v-for="(item, idx) in queueWaiting" :key="item.task_id" class="queue-row is-waiting">
          <div class="queue-row-main">
            <div class="queue-row-top">
              <span class="queue-status q-status-queued">等待 {{ idx + 1 }}</span>
              <span class="queue-link" :title="item.link">{{ linkLabel(item) }}</span>
              <button class="queue-redo" title="重新下载" :disabled="redoLoading" @click="redownload(item)">
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="15" height="15">
                  <polyline points="23 4 23 10 17 10"/>
                  <path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/>
                </svg>
              </button>
            </div>
            <div class="queue-queued-hint">排队中，前面还有 {{ idx }} 个</div>
          </div>
        </div>
      </div>
    </div>
    
    <div class="filter-section">
      <div class="filter-item">
        <svg class="filter-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <circle cx="11" cy="11" r="8"/>
          <path d="M21 21l-4.35-4.35"/>
        </svg>
        <input 
          type="text" 
          class="filter-input" 
          v-model="filters.keyword" 
          :placeholder="t('history.searchPlaceholder')"
          @input="debounceSearch"
        >
      </div>
      <div class="filter-item">
        <svg class="filter-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <polygon points="22 3 2 3 10 12.46 10 19 14 21 14 12.46 22 3"/>
        </svg>
        <select class="filter-select" v-model="filters.status" @change="fetchHistory(1)">
          <option value="">{{ t('history.allStatus') }}</option>
          <option value="downloading">{{ t('history.downloading') }}</option>
          <option value="completed">{{ t('history.completed') }}</option>
          <option value="failed">{{ t('history.failed') }}</option>
          <option value="queued">{{ t('history.pending') }}</option>
        </select>
      </div>
      <div class="filter-item">
        <svg class="filter-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <rect x="3" y="4" width="18" height="18" rx="2" ry="2"/>
          <line x1="16" y1="2" x2="16" y2="6"/>
          <line x1="8" y1="2" x2="8" y2="6"/>
          <line x1="3" y1="10" x2="21" y2="10"/>
        </svg>
        <input 
          type="date" 
          class="filter-input" 
          v-model="filters.date" 
          @change="fetchHistory(1)"
        >
      </div>
      <button class="filter-reset" @click="resetFilters">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
          <path d="M3 12a9 9 0 019-9 9.75 9.75 0 016.74 2.74L21 8"/>
          <path d="M21 3v5h-5"/>
          <path d="M21 12a9 9 0 01-9 9 9.75 9.75 0 01-6.74-2.74L3 16"/>
          <path d="M8 16H3v5"/>
        </svg>
        {{ t('common.reset') }}
      </button>
    </div>
    
    <div class="history-card">
      <LoadingState v-if="loading" />
      
      <EmptyState 
        v-else-if="history.length === 0" 
        :title="t('history.noRecords')" 
        :description="t('history.noRecordsDesc')"
      >
        <a href="/" class="empty-btn">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
          </svg>
          {{ t('common.goDownload') }}
        </a>
      </EmptyState>
      
      <div v-else class="history-list">
        <div v-for="item in history" :key="item.task_id" class="history-item">
          <div class="history-info">
            <a :href="'/detail/' + encodeURIComponent(item.user_id)" class="history-avatar-link">
              <div class="history-avatar">
                <img v-if="item.avatar_url" :src="item.avatar_url" :alt="item.user_name" @error="($event.target as HTMLImageElement).style.display='none'; ($event.target as HTMLImageElement).nextElementSibling?.setAttribute('style', 'display:flex')" />
                <span v-else class="avatar-fallback">{{ (item.user_name || item.user_id || '?').charAt(0).toUpperCase() }}</span>
              </div>
            </a>
            <div class="history-details">
              <div class="history-user">
                <a :href="'/detail/' + encodeURIComponent(item.user_id)" class="user-name-link">
                  {{ item.user_name || t('history.unknownUser') }}
                </a>
                <a :href="'/detail/' + encodeURIComponent(item.user_id)" class="history-user-id">@{{ item.user_id }}</a>
                <span 
                  class="status-tag" 
                  :class="'status-' + item.status"
                >
                  {{ getStatusText(item.status) }}
                  <span v-if="item.status === 'failed' && item.error_message" class="error-tooltip">
                    {{ item.error_message }}
                  </span>
                </span>
              </div>
              <div class="history-meta">
                <span class="history-meta-item">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M13 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V9z"/>
                    <polyline points="13 2 13 9 20 9"/>
                  </svg>
                  {{ item.downloaded_files || 0 }}/{{ displayTotal(item) }} {{ t('history.files') }}
                </span>
                <span v-if="item.folder_size" class="history-meta-item">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
                    <polyline points="7 10 12 15 17 10"/>
                    <line x1="12" y1="15" x2="12" y2="3"/>
                  </svg>
                  {{ formatFileSize(item.folder_size) }}
                </span>
                <span v-if="item.link" class="history-meta-item history-link-item" :title="item.link">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/>
                    <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>
                  </svg>
                  {{ linkLabel(item) }}
                </span>
                <span class="history-meta-item">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <circle cx="12" cy="12" r="10"/>
                    <polyline points="12 6 12 12 16 14"/>
                  </svg>
                  {{ item.created_at || '-' }}
                </span>
              </div>
            </div>
          </div>
          <div class="history-actions">
            <button 
              v-if="item.status === 'completed' && item.zip_path"
              class="action-btn action-download"
              @click="downloadFile(item.task_id)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-4l-4 4m0 0l-4-4m4 4V4"/>
              </svg>
              {{ t('history.download') }}
            </button>
            <button 
              v-if="item.status === 'completed' && !item.zip_path"
              class="action-btn action-zip"
              @click="createZip(item.user_id)"
              :disabled="zippingUsers.includes(item.user_id)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
              {{ zippingUsers.includes(item.user_id) ? t('history.zipping') : t('history.createZip') }}
            </button>
            <button 
              class="action-btn action-redo"
              :disabled="redoLoading"
              @click="redownload(item)"
              :title="item.link || '重新下载'"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="23 4 23 10 17 10"/>
                <path d="M20.49 15a9 9 0 11-2.12-9.36L23 10"/>
              </svg>
              重新下载
            </button>
            <button 
              class="action-btn action-delete"
              @click="deleteHistory(item.task_id)"
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 01-2 2H7a2 2 0 01-2-2V6m3 0V4a2 2 0 012-2h4a2 2 0 012 2v2"/>
              </svg>
              {{ t('common.delete') }}
            </button>
          </div>
        </div>
      </div>
      
      <Pagination 
        v-if="total > pageSize"
        :current-page="currentPage"
        :total-pages="totalPages"
        :total="total"
        @page-change="fetchHistory"
      />
    </div>
  </div>
</template>

<style scoped>
.queue-panel { padding: 16px 18px; }
.queue-panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 4px;
}
.queue-panel-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 15px;
  font-weight: 700;
  color: #1F2937;
}
.queue-panel-title svg { color: #4A6CF7; }
.queue-panel-badge {
  padding: 3px 10px;
  border-radius: 20px;
  background: #EFF4FF;
  color: #4A6CF7;
  font-size: 12px;
  font-weight: 600;
}
.queue-row {
  display: flex;
  padding: 12px;
  border-radius: 12px;
  border: 1px solid #F0F1F3;
  background: #FAFBFD;
  margin-top: 8px;
}
.queue-row.is-waiting { background: #FBFAF4; }
.queue-row-main { flex: 1; min-width: 0; }
.queue-row-top { display: flex; align-items: center; gap: 10px; }
.queue-status {
  flex-shrink: 0;
  padding: 2px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 600;
  white-space: nowrap;
}
.q-status-downloading { background: #DBEAFE; color: #2563EB; }
.q-status-queued { background: #FEF3C7; color: #D97706; }
.q-status-completed { background: #D1FAE5; color: #059669; }
.q-status-failed { background: #FEE2E2; color: #DC2626; }
.queue-link {
  flex: 1;
  min-width: 0;
  font-size: 14px;
  color: #1F2937;
  font-weight: 500;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.queue-redo {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border: none;
  background: transparent;
  color: #9CA3AF;
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  transition: all 0.2s;
}
.queue-redo:hover:not(:disabled) { background: #EFF4FF; color: #4A6CF7; }
.queue-redo:disabled { opacity: 0.5; cursor: not-allowed; }
.queue-progress-line {
  margin-top: 10px;
  height: 6px;
  background: #EEF0F4;
  border-radius: 3px;
  overflow: hidden;
}
.queue-progress-fill {
  height: 100%;
  border-radius: 3px;
  background: linear-gradient(90deg, #4A6CF7, #7B61FF);
  transition: width 0.3s ease;
}
.queue-row-foot {
  margin-top: 8px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 12px;
  color: #9CA3AF;
}
.queue-queued-hint {
  margin-top: 8px;
  font-size: 12px;
  color: #B6BEC9;
}
.history-link-item {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.action-redo {
  color: #4A6CF7;
}
.action-redo:hover {
  background: #EFF4FF;
  border-color: #4A6CF7;
}
.action-redo:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
