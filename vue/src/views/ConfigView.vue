<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import Navbar from '../components/Navbar.vue'
import PageHeader from '../components/PageHeader.vue'
import i18n from '../utils/i18n'
import { ElMessage } from 'element-plus'

const t = (key: string, params: Record<string, any> = {}) => i18n.t(key, params)

const configList = ref<any[]>([])
const loading = ref(false)
const saving = ref(false)
const deviceToken = ref('')
const tokenLoading = ref(false)

const fetchConfigs = async () => {
  loading.value = true
  try {
    const response = await fetch('/api/configs')
    const data = await response.json()
    configList.value = data
  } catch (error) {
    ElMessage.error(t('config.fetchFailed'))
  } finally {
    loading.value = false
  }
}

const fetchDeviceToken = async () => {
  tokenLoading.value = true
  try {
    const response = await fetch('/api/device/token')
    if (response.ok) {
      const data = await response.json()
      deviceToken.value = data.token || ''
    }
  } catch (error) {
    ElMessage.error('获取设备令牌失败')
  } finally {
    tokenLoading.value = false
  }
}

const copyToken = async () => {
  if (!deviceToken.value) return
  try {
    await navigator.clipboard.writeText(deviceToken.value)
    ElMessage.success('令牌已复制')
  } catch (error) {
    ElMessage.error('复制失败，请手动长按复制')
  }
}

const rotateToken = async () => {
  try {
    const response = await fetch('/api/device/token', { method: 'POST' })
    if (response.ok) {
      const data = await response.json()
      deviceToken.value = data.token || ''
      ElMessage.success('已生成新令牌，旧令牌立即失效，请在手机App中更新')
    } else {
      ElMessage.error('生成失败')
    }
  } catch (error) {
    ElMessage.error('生成失败')
  }
}

const webdavBusy = ref(false)
const webdavResult = ref('')

const WEBDAV_KEYS = new Set(['webdav_url', 'webdav_username', 'webdav_password', 'webdav_encrypt_pass', 'webdav_dir', 'webdav_enabled'])

// 可编辑配置列表：WebDAV 相关字段已在 WebDAV 卡片内单独展示，从通用列表隐藏
const editableConfigs = computed(() => configList.value.filter((c: any) => !WEBDAV_KEYS.has(c.key)))

// 按 key 返回配置项（不存在则补一条），供 WebDAV 卡片内的输入框双向绑定
const cfgRef = (key: string) => {
  let it = configList.value.find((c: any) => c.key === key)
  if (!it) { it = { key, value: '', description: '', updated_at: '' }; configList.value.push(it) }
  return it
}

const runWebdav = async (action: string) => {
  // 先把当前输入（含 webdav_* 字段）保存，确保后端用到最新值
  const data: Record<string, any> = {}
  configList.value.forEach((c: any) => { data[c.key] = c.value })
  try {
    await fetch('/api/configs', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
    })
  } catch (e) { /* 忽略，交给后续接口报错 */ }

  webdavBusy.value = true
  try {
    const headers = { 'Content-Type': 'application/json' }
    let url = '/api/webdav/test'
    let body: any = undefined
    if (action === 'push' || action === 'pull') {
      url = '/api/webdav/sync'
      body = JSON.stringify({ action })
    }
    const r = await fetch(url, { method: 'POST', headers, body })
    const j = await r.json()
    if (r.ok && j.ok) {
      webdavResult.value = action === 'test'
        ? '✅ ' + (j.message || '连接成功')
        : action === 'push'
          ? `✅ 已上传（头像/缩略图 ${j.files_uploaded ?? 0} 个）`
          : `✅ 已拉取（还原配置:${!!j.restored_config} 历史:${!!j.restored_history} 媒体:${j.media_pulled ?? 0}）`
    } else {
      webdavResult.value = '❌ ' + (j.error || '操作失败')
    }
  } catch (error: any) {
    webdavResult.value = '❌ ' + error.message
  } finally {
    webdavBusy.value = false
  }
}

const saveConfigs = async () => {
  saving.value = true
  try {
    const data: Record<string, any> = {}
    configList.value.forEach(cfg => {
      data[cfg.key] = cfg.value
    })
    
    const response = await fetch('/api/configs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(data)
    })
    
    if (response.ok) {
      ElMessage.success(t('config.configSaved'))
      fetchConfigs()
    } else {
      ElMessage.error(t('config.saveFailed'))
    }
  } catch (error: any) {
    ElMessage.error(t('config.saveFailed') + ': ' + error.message)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  fetchConfigs()
  fetchDeviceToken()
})
</script>

<template>
  <Navbar active-page="config" />
  
  <div class="main-container">
    <PageHeader 
      :title="t('config.title')" 
      :subtitle="t('config.subtitle')"
      icon="<circle cx='12' cy='12' r='3'/><path d='M12 1v4m0 14v4M4.22 4.22l2.83 2.83m9.9 9.9l2.83 2.83M1 12h4m14 0h4M4.22 19.78l2.83-2.83m9.9-9.9l2.83-2.83'/>"
    />
    
    <!-- 设备联动令牌卡片 -->
    <div class="config-card" style="margin-top: 16px; border: 1px solid #1DA1F2;">
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <div style="display: flex; align-items: center; gap: 8px;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#1DA1F2" stroke-width="2">
            <rect x="3" y="11" width="18" height="11" rx="2"/>
            <path d="M7 11V7a5 5 0 0110 0v4"/>
          </svg>
          <span style="font-weight: 600;">设备联动令牌（安卓App推送链接用）</span>
        </div>
      </div>
      <div style="color: #666; font-size: 13px; margin-bottom: 10px; line-height: 1.6;">
        手机安装「链接推送器」App 后，把下方令牌填入 App，即可在手机上复制链接自动推送到电脑下载。
      </div>
      <div style="display: flex; gap: 10px; align-items: center;">
        <code style="flex: 1; background: #f0f4f8; padding: 10px 12px; border-radius: 6px; font-size: 13px; word-break: break-all;"
          v-loading="tokenLoading">{{ deviceToken || '加载中…' }}</code>
        <button class="save-btn" style="width: auto; padding: 0 16px; white-space: nowrap;" @click="copyToken">复制</button>
        <button class="save-btn" style="width: auto; padding: 0 16px; white-space: nowrap; background: #fff; color: #C62828; border: 1px solid #C62828;" @click="rotateToken">重新生成</button>
      </div>
    </div>

    <!-- WebDAV 同步卡片 -->
    <div class="config-card" style="margin-top: 16px; border: 1px solid #6A5ACD;">
      <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px;">
        <div style="display: flex; align-items: center; gap: 8px;">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#6A5ACD" stroke-width="2">
            <path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>
            <circle cx="12" cy="13" r="3"/>
          </svg>
          <span style="font-weight: 600;">WebDAV 同步</span>
        </div>
      </div>
      <div style="color: #666; font-size: 13px; margin-bottom: 12px; line-height: 1.6;">
        填入 WebDAV 服务器的地址与账号，保存后即可把「配置(cookie/代理) + 下载历史记录 + 头像/缩略图」同步到云端；<b>下载的视频不上传</b>。换机时在另一台电脑填入相同地址与账号即可还原。
      </div>

      <div style="display: flex; flex-direction: column; gap: 10px; margin-bottom: 12px;">
        <div>
          <label style="font-size: 13px; color: #333; display: block; margin-bottom: 4px;">服务器地址 (webdav_url)</label>
          <input class="config-input" v-model="cfgRef('webdav_url').value" placeholder="https://dav.example.com/remote.php/dav" @keyup.enter="saveConfigs">
        </div>
        <div>
          <label style="font-size: 13px; color: #333; display: block; margin-bottom: 4px;">账号 (webdav_username)</label>
          <input class="config-input" v-model="cfgRef('webdav_username').value" placeholder="用户名 / 账号" @keyup.enter="saveConfigs">
        </div>
        <div>
          <label style="font-size: 13px; color: #333; display: block; margin-bottom: 4px;">密码 (webdav_password)</label>
          <input class="config-input" type="password" v-model="cfgRef('webdav_password').value" placeholder="密码 / 应用专用密码" @keyup.enter="saveConfigs">
        </div>
        <div style="display: flex; align-items: center; gap: 8px;">
          <input type="checkbox" id="webdav-enable" v-model="cfgRef('webdav_enabled').value" :true-value="'1'" :false-value="''" style="accent-color:#6A5ACD; width:16px; height:16px;">
          <label for="webdav-enable" style="font-size: 13px; color: #333;">启用 WebDAV 同步</label>
        </div>
      </div>
      <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
        <button class="save-btn" style="width: auto; padding: 0 16px; white-space: nowrap;" :disabled="webdavBusy" @click="runWebdav('test')">测试连接</button>
        <button class="save-btn" style="width: auto; padding: 0 16px; white-space: nowrap;" :disabled="webdavBusy" @click="runWebdav('push')">立即上传</button>
        <button class="save-btn" style="width: auto; padding: 0 16px; white-space: nowrap;" :disabled="webdavBusy" @click="runWebdav('pull')">立即还原</button>
      </div>
      <div v-if="webdavResult" style="margin-top: 10px; font-size: 13px; color: #333; word-break: break-all;">{{ webdavResult }}</div>
    </div>

    <!-- 配置卡片 -->
    <div class="config-card" v-loading="loading">
      <div v-for="(cfg, index) in editableConfigs" :key="cfg.key" class="config-item">
        <div class="label-row">
          <label>
            <svg class="label-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <circle cx="12" cy="12" r="3"/>
              <path d="M12 1v4m0 14v4M4.22 4.22l2.83 2.83m9.9 9.9l2.83 2.83M1 12h4m14 0h4M4.22 19.78l2.83-2.83m9.9-9.9l2.83-2.83"/>
            </svg>
            {{ cfg.description || cfg.key }}
          </label>
          <div v-if="cfg.key === 'auth_token' || cfg.key === 'ct0'" class="tip-wrapper">
            <span class="tip-tag">
              <el-icon><Warning /></el-icon>
              {{ t('config.howToGet') }}
            </span>
            <div class="tip-content">
              <div class="tip-content-title">{{ t('config.howToGetTitle') }}</div>
              <ul class="tip-content-list">
                <li v-for="step in t('config.howToGetSteps')" :key="step">{{ step }}</li>
              </ul>
            </div>
          </div>
        </div>
        <input
          class="config-input"
          v-model="configList[index].value"
          :placeholder="cfg.key === 'auth_token' ? t('config.authTokenPlaceholder') : (cfg.key === 'ct0' ? t('config.ct0Placeholder') : t('config.inputPlaceholder') + (cfg.description || cfg.key))"
          @keyup.enter="saveConfigs"
        >
        <div v-if="cfg.updated_at" class="config-hint">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <circle cx="12" cy="12" r="10"/>
            <path d="M12 6v6l4 2"/>
          </svg>
          {{ t('config.lastUpdated') }}{{ cfg.updated_at }}
        </div>
      </div>
      
      <button 
        class="save-btn"
        :disabled="saving"
        @click="saveConfigs"
      >
        <span v-if="saving" class="btn-spinner"></span>
        <svg v-else width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
          <path d="M19 21H5a2 2 0 01-2-2V5a2 2 0 012-2h11l5 5v11a2 2 0 01-2 2z"/>
          <polyline points="17 21 17 13 7 13 7 21"/>
          <polyline points="7 3 7 8 15 8"/>
        </svg>
        {{ saving ? t('config.saving') : t('config.saveConfig') }}
      </button>
    </div>
  </div>
</template>

<style scoped>
</style>