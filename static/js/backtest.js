(() => {
    const byId = (id) => document.getElementById(id);
    const form = byId('backtest-form');
    const loading = byId('backtest-loading');
    const error = byId('backtest-error');
    const result = byId('backtest-result');
    const money = (value) => new Intl.NumberFormat('zh-CN', {
        style: 'currency', currency: 'CNY', minimumFractionDigits: 2,
    }).format(Number(value || 0));
    const percent = (value) => `${Number(value || 0) >= 0 ? '+' : ''}${Number(value || 0).toFixed(2)}%`;
    const escapeHtml = (value) => String(value ?? '').replace(/[&<>'"]/g, (character) => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;',
    })[character]);

    const render = (payload) => {
        const data = payload.result;
        const summary = data.summary;
        const signal = data.latest_signal;
        byId('strategy-name').textContent = data.strategy_name;
        byId('backtest-period').textContent = `${data.period.start} 至 ${data.period.end} · ${data.period.trading_days} 个交易日 · 信号日收盘价模拟`;
        byId('total-value').textContent = money(summary.total_value);
        byId('total-return').textContent = `账户收益 ${percent(summary.return_rate)}`;
        byId('profit').textContent = money(summary.profit);
        byId('annualized-return').textContent = `年化收益 ${percent(summary.annualized_return)}`;
        byId('cash').textContent = money(summary.cash);
        byId('position').textContent = `持仓 ${Number(summary.position).toFixed(4)} 克 · 市值 ${money(summary.position_value)}`;
        byId('trade-count').textContent = `${summary.executed_trade_count} 笔`;
        byId('skipped-count').textContent = `${summary.skipped_signal_count} 个跳过信号`;
        const latestWeight = signal.signal_weight ? ` · ${escapeHtml(signal.signal_weight)}× 共振` : '';
        byId('latest-signal').innerHTML = `<span>最新信号 · ${escapeHtml(signal.date)}</span><strong>${escapeHtml(signal.action)}${latestWeight}</strong><p>${escapeHtml(signal.reason)}</p>`;

        const rows = data.trades.map((trade) => `<tr>
            <td>${escapeHtml(trade.date)}</td>
            <td><span class="trade-action ${escapeHtml(trade.action)}">${escapeHtml(trade.action)}</span></td>
            <td>${trade.execution_status === 'executed' ? '已成交' : '跳过'}</td>
            <td>${Number(trade.signal_weight || 1)}×</td>
            <td>${Number(trade.price).toFixed(2)}</td>
            <td>${money(trade.amount)}</td>
            <td>${Number(trade.quantity).toFixed(4)}</td>
            <td>${escapeHtml(trade.reason)}</td>
        </tr>`).join('');
        byId('trades-body').innerHTML = rows || '<tr><td colspan="8">所选区间没有该策略的交叉交易信号。</td></tr>';
        result.hidden = false;
    };

    form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const initialCash = Number(byId('initial-cash').value);
        const orderAmount = Number(byId('order-amount').value);
        if (!Number.isFinite(initialCash) || initialCash <= 0 || !Number.isFinite(orderAmount) || orderAmount <= 0) {
            error.textContent = '初始资金和基础单笔金额必须是大于 0 的有效数字。';
            error.hidden = false;
            return;
        }
        if (orderAmount > initialCash) {
            error.textContent = '基础单笔金额不能高于初始资金。';
            error.hidden = false;
            return;
        }
        const query = new URLSearchParams();
        for (const [key, value] of new FormData(form).entries()) {
            if (value) query.set(key, value);
        }
        error.hidden = true;
        result.hidden = true;
        loading.hidden = false;
        try {
            const response = await fetch(`/api/gold/backtest?${query.toString()}`);
            const payload = await response.json();
            if (!response.ok || payload.status !== 'success') throw new Error(payload.message || '回测请求失败');
            render(payload);
        } catch (requestError) {
            error.textContent = `回测失败：${requestError.message}`;
            error.hidden = false;
        } finally {
            loading.hidden = true;
        }
    });

    form.requestSubmit();
})();
