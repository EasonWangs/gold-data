// 黄金价格服务前端应用
const { createApp } = Vue;

createApp({
    data() {
        return {
            activeTab: 'price',
            tabs: [
                { id: 'price', name: '📊 价格数据' },
                { id: 'push', name: '🔔 钉钉推送' },
                { id: 'api', name: '🔌 API文档' }
            ],
            loading: {
                realtime: false,
                history: false,
                push: false
            },
            realtimeData: null,
            realtimeResponse: null,
            realtimeError: null,
            historyData: [],
            historyDays: 5,
            historyError: null,
            adminToken: '',
            pushMessage: null
        }
    },
    mounted() {
    },
    methods: {
        adminRequestConfig() {
            return {
                headers: {
                    'X-Admin-Token': this.adminToken
                }
            };
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

        async testPush() {
            await this.performPush('/api/push/test', '测试推送');
        },

        async pushOpening() {
            await this.performPush('/api/push/opening', '推送开盘价');
        },

        async pushClosing() {
            await this.performPush('/api/push/closing', '推送收盘价');
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
