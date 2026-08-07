// 黄金价格服务前端应用
const { createApp } = Vue;
const PUSH_CONFIG_CACHE_KEY = 'gold-push-channel-state';

createApp({
    data() {
        return {
            activeTab: 'price',
            tabs: [
                { id: 'price', name: '📊 价格数据' },
                { id: 'push', name: '🔔 推送管理' },
                { id: 'api', name: '🔌 API文档' }
            ],
            loading: {
                realtime: false,
                history: false,
                silverRealtime: false,
                silverHistory: false,
                push: false,
                pushConfig: false
            },
            realtimeData: null,
            realtimeResponse: null,
            realtimeError: null,
            historyData: [],
            historyDays: 5,
            historyError: null,
            silverRealtimeData: null,
            silverRealtimeResponse: null,
            silverRealtimeError: null,
            silverHistoryData: [],
            silverHistoryDays: 5,
            silverHistoryError: null,
            adminToken: '',
            pushMessage: null,
            pushConfigMessage: null,
            pushConfig: {
                dingtalk: {
                    enabled: false,
                    webhook_url: '',
                    webhook_configured: false,
                    link_url: ''
                },
                feishu: {
                    enabled: false,
                    webhook_url: '',
                    webhook_configured: false,
                    secret: '',
                    secret_configured: false,
                    link_url: ''
                }
            }
        }
    },
    mounted() {
        this.restoreCachedPushConfig();
    },
    methods: {
        adminRequestConfig() {
            return {
                headers: {
                    'X-Admin-Token': this.adminToken
                }
            };
        },

        cachePushConfig(channels) {
            try {
                sessionStorage.setItem(PUSH_CONFIG_CACHE_KEY, JSON.stringify(channels));
            } catch {
                // Private browsing or browser policy can disable session storage.
            }
        },

        restoreCachedPushConfig() {
            try {
                const cached = sessionStorage.getItem(PUSH_CONFIG_CACHE_KEY);
                if (cached) {
                    this.applyPushConfig(JSON.parse(cached));
                }
            } catch {
                // Ignore malformed or unavailable browser storage.
            }
        },

        async refreshRealTimePrice() {
            this.loading.realtime = true;
            this.realtimeError = null;

            try {
                const response = await axios.get('/api/gold/spot_quotations_sge');
                this.realtimeResponse = response.data;
                if (response.data.status === 'success' && response.data.data.length > 0) {
                    this.realtimeData = response.data.data[response.data.data.length - 1];
                } else {
                    this.realtimeError = '无法获取实时价格数据';
                }
            } catch (error) {
                this.realtimeError = '网络请求失败: ' + (error.response?.data?.message || error.message);
            } finally {
                this.loading.realtime = false;
            }
        },

        async refreshHistoryPrice() {
            this.loading.history = true;
            this.historyError = null;

            try {
                const response = await axios.get(`/api/gold/spot_hist_sge?days=${this.historyDays}`);
                if (response.data.status === 'success') {
                    this.historyData = response.data.data;
                } else {
                    this.historyError = '无法获取历史价格数据';
                }
            } catch (error) {
                this.historyError = '网络请求失败: ' + (error.response?.data?.message || error.message);
            } finally {
                this.loading.history = false;
            }
        },

        async refreshSilverRealTimePrice() {
            this.loading.silverRealtime = true;
            this.silverRealtimeError = null;

            try {
                const response = await axios.get('/api/silver/spot_quotations_sge');
                this.silverRealtimeResponse = response.data;
                if (response.data.status === 'success' && response.data.data.length > 0) {
                    this.silverRealtimeData = response.data.data[response.data.data.length - 1];
                } else {
                    this.silverRealtimeError = '无法获取实时白银价格数据';
                }
            } catch (error) {
                this.silverRealtimeError = '网络请求失败: ' + (error.response?.data?.message || error.message);
            } finally {
                this.loading.silverRealtime = false;
            }
        },

        async refreshSilverHistoryPrice() {
            this.loading.silverHistory = true;
            this.silverHistoryError = null;

            try {
                const response = await axios.get(`/api/silver/spot_hist_sge?days=${this.silverHistoryDays}`);
                if (response.data.status === 'success') {
                    this.silverHistoryData = response.data.data;
                } else {
                    this.silverHistoryError = '无法获取历史白银价格数据';
                }
            } catch (error) {
                this.silverHistoryError = '网络请求失败: ' + (error.response?.data?.message || error.message);
            } finally {
                this.loading.silverHistory = false;
            }
        },

        async testPush() {
            await this.performPush('/api/push/test', '测试所有已启用渠道');
        },

        applyPushConfig(channels) {
            this.pushConfig = {
                dingtalk: {
                    ...channels.dingtalk,
                    webhook_url: ''
                },
                feishu: {
                    ...channels.feishu,
                    webhook_url: '',
                    secret: ''
                }
            };
            this.cachePushConfig(channels);
        },

        async loadPushConfig() {
            if (!this.adminToken) return;
            this.loading.pushConfig = true;
            this.pushConfigMessage = null;
            try {
                const response = await axios.get('/api/push/config', this.adminRequestConfig());
                this.applyPushConfig(response.data.channels);
                this.pushConfigMessage = { type: 'success', text: '渠道配置已读取；密钥不会显示，请留空以保持原值。' };
            } catch (error) {
                this.pushConfigMessage = {
                    type: 'error',
                    text: '读取渠道配置失败: ' + (error.response?.data?.message || error.message)
                };
            } finally {
                this.loading.pushConfig = false;
            }
        },

        async savePushConfig() {
            this.loading.pushConfig = true;
            this.pushConfigMessage = null;
            try {
                const response = await axios.put('/api/push/config', this.pushConfig, this.adminRequestConfig());
                this.applyPushConfig(response.data.channels);
                this.pushConfigMessage = { type: 'success', text: response.data.message };
            } catch (error) {
                this.pushConfigMessage = {
                    type: 'error',
                    text: '保存渠道配置失败: ' + (error.response?.data?.message || error.message)
                };
            } finally {
                this.loading.pushConfig = false;
            }
        },

        async testFeishuPush() {
            await this.performPush('/api/push/test/feishu', '测试飞书推送');
        },

        async pushOpening() {
            await this.performPush('/api/push/opening', '推送开盘价');
        },

        async pushClosing() {
            await this.performPush('/api/push/closing?mode=latest', '模拟推送收盘价');
        },

        async testKdjSignalPush() {
            await this.performPush('/api/push/kdj-signal', '测试 KDJ 策略推送');
        },

        async performPush(endpoint, action) {
            this.loading.push = true;
            this.pushMessage = null;

            try {
                const response = await axios.post(endpoint, {}, this.adminRequestConfig());
                this.pushMessage = {
                    type: response.data.status === 'success' ? 'success' : 'error',
                    text: response.data.message
                };
            } catch (error) {
                this.pushMessage = {
                    type: 'error',
                    text: `${action}失败: ` + (error.response?.data?.message || error.message)
                };
            } finally {
                this.loading.push = false;
                setTimeout(() => this.pushMessage = null, 5000);
            }
        },

        formatTimestamp(timestamp) {
            if (!timestamp) return '-';
            try {
                return new Date(timestamp).toLocaleString('zh-CN', {
                    year: 'numeric',
                    month: '2-digit',
                    day: '2-digit',
                    hour: '2-digit',
                    minute: '2-digit',
                    second: '2-digit'
                });
            } catch {
                return timestamp;
            }
        },

        formatDate(dateStr) {
            if (!dateStr) return 'N/A';
            try {
                return dateStr.split('T')[0];
            } catch {
                return dateStr;
            }
        }
    }
}).mount('#app');
