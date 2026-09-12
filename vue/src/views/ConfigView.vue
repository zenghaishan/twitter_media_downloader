<script setup lang="ts">
import { ref, onMounted } from 'vue'
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

    <!-- 配置卡片 -->
    <div class="config-card" v-loading="loading">
      <div v-for="(cfg, index) in configList" :key="cfg.key" class="config-item">
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