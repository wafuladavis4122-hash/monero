    // ========== GLOBAL VARIABLES ==========
    let currentTab = '{{ active_tab|default:"dashboard" }}';
    let chartInstance = null;
    let tokenModal = null;
    let userDetailsModal = null;
    let adjustBalanceModal = null;
    let investmentDetailsModal = null;
    let transactionDetailsModal = null;
    let imagePreviewModal = null;
    let confirmModal = null;
    let systemStatusModal = null;
    let maintenanceModal = null;
    let currentAction = null;
    let currentItemId = null;
    let currentUserId = null;
    let currentInvestmentId = null;
    let currentItemData = null;

    // API URLs from Django
    const API_URL = '{% url "XMR:admin_api" %}';
    const CATCH_UP_URL = '{% url "XMR:admin_catch_up_payouts" %}';
    const PAYOUT_STATS_URL = '{% url "XMR:admin_payout_stats" %}';
    const SYSTEM_STATUS_URL = '{% url "XMR:admin_system_status" %}';
    const FIX_WALLETS_URL = '{% url "XMR:fix_all_wallets" %}';
    const EXPORT_USERS_URL = '{% url "XMR:export_users_csv" %}';
    const EXPORT_TRANSACTIONS_URL = '{% url "XMR:export_transactions_csv" %}';
    const CSRF_TOKEN = '{{ csrf_token }}';

    // ========== INITIALIZATION ==========
    document.addEventListener('DOMContentLoaded', function() {
        // Initialize Bootstrap modals
        tokenModal = new bootstrap.Modal(document.getElementById('tokenModal'));
        userDetailsModal = new bootstrap.Modal(document.getElementById('userDetailsModal'));
        adjustBalanceModal = new bootstrap.Modal(document.getElementById('adjustBalanceModal'));
        investmentDetailsModal = new bootstrap.Modal(document.getElementById('investmentDetailsModal'));
        transactionDetailsModal = new bootstrap.Modal(document.getElementById('transactionDetailsModal'));
        imagePreviewModal = new bootstrap.Modal(document.getElementById('imagePreviewModal'));
        confirmModal = new bootstrap.Modal(document.getElementById('confirmModal'));
        systemStatusModal = new bootstrap.Modal(document.getElementById('systemStatusModal'));
        maintenanceModal = new bootstrap.Modal(document.getElementById('maintenanceModal'));

        // Initialize Select2
        $('.filter-select').select2({
            theme: 'bootstrap-5',
            width: '100%'
        });

        // Initialize Date Range Pickers
        $('.date-range-picker').daterangepicker({
            autoUpdateInput: false,
            locale: {
                cancelLabel: 'Clear',
                format: 'YYYY-MM-DD'
            },
            ranges: {
                'Today': [moment(), moment()],
                'Yesterday': [moment().subtract(1, 'days'), moment().subtract(1, 'days')],
                'Last 7 Days': [moment().subtract(6, 'days'), moment()],
                'This Month': [moment().startOf('month'), moment().endOf('month')],
                'Last Month': [moment().subtract(1, 'month').startOf('month'), moment().subtract(1, 'month').endOf('month')]
            }
        });

        $('.date-range-picker').on('apply.daterangepicker', function(ev, picker) {
            $(this).val(picker.startDate.format('YYYY-MM-DD') + ' to ' + picker.endDate.format('YYYY-MM-DD'));
        });

        $('.date-range-picker').on('cancel.daterangepicker', function(ev, picker) {
            $(this).val('');
        });

        // Load daily deposits chart if on dashboard
        if (document.getElementById('depositsChart')) {
            loadDepositsChart();
        }

        // Add event listeners
        document.getElementById('themeToggle').addEventListener('click', toggleTheme);

        // Check if we need to switch to a tab from URL hash
        if (window.location.hash) {
            const tabId = window.location.hash.substring(1);
            if (document.getElementById(tabId)) {
                switchTab(tabId);
            }
        }
        
        // Load initial payout stats
        refreshPayoutStats();
        refreshSystemHealth();
        
        // Set up periodic refresh
        setInterval(refreshPayoutStats, 30000);
        setInterval(refreshSystemHealth, 60000);
    });

    // ========== THEME TOGGLE ==========
    function toggleTheme() {
        const html = document.documentElement;
        const currentTheme = html.getAttribute('data-bs-theme');
        const newTheme = currentTheme === 'light' ? 'dark' : 'light';
        html.setAttribute('data-bs-theme', newTheme);
        localStorage.setItem('admin-theme', newTheme);
        
        // Update chart colors if chart exists
        if (chartInstance) {
            chartInstance.destroy();
            loadDepositsChart();
        }
    }

    // Load saved theme
    const savedTheme = localStorage.getItem('admin-theme') || 'dark';
    document.documentElement.setAttribute('data-bs-theme', savedTheme);

    // ========== TAB SWITCHING ==========
    function switchTab(tabId) {
        document.querySelectorAll('.tab-pane').forEach(tab => {
            tab.classList.remove('active');
        });
        
        const selectedTab = document.getElementById(tabId);
        if (selectedTab) {
            selectedTab.classList.add('active');
        }
        
        document.querySelectorAll('.nav-link-sidebar').forEach(link => {
            link.classList.remove('active');
        });
        
        const activeLink = Array.from(document.querySelectorAll('.nav-link-sidebar')).find(
            link => link.textContent.toLowerCase().includes(tabId.replace(/-/g, ' '))
        );
        if (activeLink) {
            activeLink.classList.add('active');
        }
        
        window.location.hash = tabId;
        currentTab = tabId;
        
        // Refresh data for specific tabs
        if (tabId === 'payouts') {
            refreshPayoutStats();
        } else if (tabId === 'dashboard') {
            refreshDashboard();
        }
    }

    // ========== CHART LOADING ==========
    function loadDepositsChart() {
        const canvas = document.getElementById('depositsChart');
        if (!canvas) return;
        
        {% if daily_deposits %}
        const dailyDeposits = {{ daily_deposits|safe }};
        {% else %}
        const dailyDeposits = [];
        {% endif %}
        
        if (chartInstance) {
            chartInstance.destroy();
        }
        
        const ctx = canvas.getContext('2d');
        const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
        
        chartInstance = new Chart(ctx, {
            type: 'line',
            data: {
                labels: dailyDeposits.map(d => d.date),
                datasets: [{
                    label: 'Deposits (KSH)',
                    data: dailyDeposits.map(d => d.total),
                    borderColor: '#f15a24',
                    backgroundColor: 'rgba(241, 90, 36, 0.1)',
                    borderWidth: 3,
                    fill: true,
                    tension: 0.4,
                    pointBackgroundColor: '#f15a24',
                    pointBorderColor: isDark ? '#1e293b' : '#ffffff',
                    pointBorderWidth: 2,
                    pointRadius: 5,
                    pointHoverRadius: 8
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        backgroundColor: isDark ? '#1e293b' : '#ffffff',
                        titleColor: isDark ? '#f1f5f9' : '#1e293b',
                        bodyColor: isDark ? '#94a3b8' : '#64748b',
                        borderColor: '#f15a24',
                        borderWidth: 1,
                        callbacks: {
                            label: function(context) {
                                return `KSH ${context.parsed.y.toLocaleString()}`;
                            }
                        }
                    }
                },
                scales: {
                    y: {
                        beginAtZero: true,
                        grid: {
                            color: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)',
                        },
                        ticks: {
                            color: isDark ? '#94a3b8' : '#64748b',
                            callback: function(value) {
                                return 'KSH ' + value.toLocaleString();
                            }
                        }
                    },
                    x: {
                        grid: {
                            display: false
                        },
                        ticks: {
                            color: isDark ? '#94a3b8' : '#64748b',
                        }
                    }
                }
            }
        });
    }

    function refreshChart() {
        if (chartInstance) {
            chartInstance.destroy();
        }
        loadDepositsChart();
        showToast('Chart refreshed', 'success');
    }

    function refreshDashboard() {
        refreshPayoutStats();
        refreshChart();
    }

    // ========== TOAST NOTIFICATIONS ==========
    function showToast(message, type = 'success') {
        const toastContainer = document.getElementById('toastContainer');
        const toastId = 'toast-' + Date.now();
        
        const icon = type === 'success' ? 'bi-check-circle-fill' : 
                    type === 'error' ? 'bi-exclamation-circle-fill' : 
                    type === 'warning' ? 'bi-exclamation-triangle-fill' : 'bi-info-circle-fill';
        
        const bgClass = `toast-${type}`;
        
        const toast = document.createElement('div');
        toast.id = toastId;
        toast.className = `toast ${bgClass}`;
        toast.setAttribute('role', 'alert');
        toast.setAttribute('aria-live', 'assertive');
        toast.setAttribute('aria-atomic', 'true');
        toast.innerHTML = `
            <div class="toast-body">
                <i class="bi ${icon}"></i>
                <span>${message}</span>
            </div>
        `;
        
        toastContainer.appendChild(toast);
        
        const bsToast = new bootstrap.Toast(toast, { delay: 5000 });
        bsToast.show();
        
        toast.addEventListener('hidden.bs.toast', function() {
            toast.remove();
        });
    }

    // ========== LOADING SPINNER ==========
    function showLoading(message = 'Processing...') {
        document.getElementById('loadingText').textContent = message;
        document.getElementById('loadingSpinner').style.display = 'flex';
    }

    function hideLoading() {
        document.getElementById('loadingSpinner').style.display = 'none';
    }

    // ========== API CALLS ==========
    async function apiCall(action, data, method = 'POST') {
        showLoading();
        
        const formData = new FormData();
        formData.append('action', action);
        formData.append('csrfmiddlewaretoken', CSRF_TOKEN);
        
        for (let key in data) {
            formData.append(key, data[key]);
        }
        
        try {
            const response = await fetch(API_URL, {
                method: method,
                body: formData,
                credentials: 'same-origin'
            });
            
            if (!response.ok) {
                throw new Error(`HTTP error! status: ${response.status}`);
            }
            
            const result = await response.json();
            hideLoading();
            
            if (result.success) {
                showToast(result.message || 'Action completed successfully', 'success');
            } else {
                showToast(result.error || 'An error occurred', 'error');
            }
            
            return result;
        } catch (error) {
            hideLoading();
            showToast('Network error: ' + error.message, 'error');
            console.error('API Error:', error);
            return { success: false, error: error.message };
        }
    }

    // ========== NOTIFICATION COUNTS ==========
    function updateNotificationCounts() {
        const deposits = document.querySelectorAll('#depositsTableBody tr .status-badge.pending').length;
        const withdrawals = document.querySelectorAll('#withdrawalsTableBody tr .status-badge.pending').length;
        const kyc = document.querySelectorAll('.kyc-card').length;
        
        const total = deposits + withdrawals + kyc;
        
        document.getElementById('notificationCount').textContent = total;
        document.getElementById('pendingDepositsCount').textContent = deposits;
        document.getElementById('pendingWithdrawalsCount').textContent = withdrawals;
        document.getElementById('pendingKycCount').textContent = kyc;
        
        document.getElementById('sidebarDepositsCount').textContent = deposits;
        document.getElementById('sidebarWithdrawalsCount').textContent = withdrawals;
        document.getElementById('sidebarKycCount').textContent = kyc;
    }

    function refreshNotifications() {
        updateNotificationCounts();
        showToast('Notifications updated', 'success');
    }

    // ========== DEPOSIT ACTIONS ==========
    function filterDeposits() {
        const status = document.getElementById('depositStatusFilter').value;
        const dateRange = document.getElementById('depositDateRange').value;
        const search = document.getElementById('depositSearch').value;
        
        let url = `?deposit_status=${status}#deposits`;
        if (dateRange) url += `&date_range=${encodeURIComponent(dateRange)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        
        window.location.href = url;
    }

    async function verifyDeposit(depositId) {
        document.getElementById('confirmModalTitle').textContent = 'Verify Deposit';
        document.getElementById('confirmModalMessage').innerHTML = `
            <p>Are you sure you want to verify this deposit?</p>
            <p class="text-warning"><i class="bi bi-exclamation-triangle"></i> This will add funds to the user's wallet.</p>
        `;
        document.getElementById('confirmModalBtn').className = 'btn btn-success';
        document.getElementById('confirmModalBtn').textContent = 'Verify';
        
        currentAction = 'verifyDeposit';
        currentItemId = depositId;
        confirmModal.show();
    }

    async function rejectDeposit(depositId) {
        const reason = prompt('Enter rejection reason:');
        if (!reason) return;
        
        document.getElementById('confirmModalTitle').textContent = 'Reject Deposit';
        document.getElementById('confirmModalMessage').textContent = `Reject deposit with reason: ${reason}`;
        document.getElementById('confirmModalBtn').className = 'btn btn-danger';
        document.getElementById('confirmModalBtn').textContent = 'Reject';
        
        currentAction = 'rejectDeposit';
        currentItemId = depositId;
        currentItemData = { reason: reason };
        confirmModal.show();
    }

    function viewDepositDetails(depositId) {
        const row = document.getElementById(`deposit-${depositId}`);
        if (!row) return;
        
        const cells = row.cells;
        const depositData = {
            id: depositId,
            userId: cells[1].querySelector('small').textContent.replace('ID: ', ''),
            username: cells[1].querySelector('strong').textContent,
            amount: cells[2].textContent,
            phone: cells[3].textContent,
            code: cells[4].textContent,
            date: cells[5].textContent,
            status: cells[6].textContent.trim()
        };
        
        // You can implement a detailed view modal here
        showToast(`Deposit #${depositId}: ${depositData.amount}`, 'info');
    }

    // ========== WITHDRAWAL ACTIONS ==========
    function filterWithdrawals() {
        const status = document.getElementById('withdrawalStatusFilter').value;
        const method = document.getElementById('withdrawalMethodFilter').value;
        const dateRange = document.getElementById('withdrawalDateRange').value;
        const search = document.getElementById('withdrawalSearch').value;
        
        let url = `?withdrawal_status=${status}&withdrawal_method=${method}#withdrawals`;
        if (dateRange) url += `&date_range=${encodeURIComponent(dateRange)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        
        window.location.href = url;
    }

    async function processWithdrawal(withdrawalId) {
        document.getElementById('confirmModalTitle').textContent = 'Process Withdrawal';
        document.getElementById('confirmModalMessage').textContent = 'Mark this withdrawal as processing?';
        document.getElementById('confirmModalBtn').className = 'btn btn-info';
        document.getElementById('confirmModalBtn').textContent = 'Process';
        
        currentAction = 'processWithdrawal';
        currentItemId = withdrawalId;
        confirmModal.show();
    }

    async function completeWithdrawal(withdrawalId) {
        const transactionCode = prompt('Enter transaction code (M-Pesa/Bank):');
        if (!transactionCode) return;
        
        document.getElementById('confirmModalTitle').textContent = 'Complete Withdrawal';
        document.getElementById('confirmModalMessage').textContent = `Complete withdrawal with code: ${transactionCode}`;
        document.getElementById('confirmModalBtn').className = 'btn btn-success';
        document.getElementById('confirmModalBtn').textContent = 'Complete';
        
        currentAction = 'completeWithdrawal';
        currentItemId = withdrawalId;
        currentItemData = { code: transactionCode };
        confirmModal.show();
    }

    async function rejectWithdrawal(withdrawalId) {
        const reason = prompt('Enter rejection reason:');
        if (!reason) return;
        
        document.getElementById('confirmModalTitle').textContent = 'Reject Withdrawal';
        document.getElementById('confirmModalMessage').textContent = `Reject withdrawal with reason: ${reason}`;
        document.getElementById('confirmModalBtn').className = 'btn btn-danger';
        document.getElementById('confirmModalBtn').textContent = 'Reject';
        
        currentAction = 'rejectWithdrawal';
        currentItemId = withdrawalId;
        currentItemData = { reason: reason };
        confirmModal.show();
    }

    function viewWithdrawalDetails(withdrawalId) {
        const row = document.getElementById(`withdrawal-${withdrawalId}`);
        if (!row) return;
        
        const cells = row.cells;
        const withdrawalData = {
            id: withdrawalId,
            requestId: cells[0].textContent,
            userId: cells[1].querySelector('small').textContent.replace('ID: ', ''),
            username: cells[1].querySelector('strong').textContent,
            amount: cells[2].textContent,
            net: cells[3].textContent,
            tax: cells[4].textContent,
            method: cells[5].textContent,
            details: cells[6].textContent,
            date: cells[7].textContent,
            status: cells[8].textContent.trim()
        };
        
        showToast(`Withdrawal #${withdrawalId}: ${withdrawalData.amount} (${withdrawalData.status})`, 'info');
    }

    // ========== INVESTMENT ACTIONS ==========
    function filterInvestments() {
        const status = document.getElementById('investmentStatusFilter').value;
        const token = document.getElementById('investmentTokenFilter').value;
        const dateRange = document.getElementById('investmentDateRange').value;
        const search = document.getElementById('investmentSearch').value;
        
        let url = `?investment_status=${status}&investment_token=${token}#investments`;
        if (dateRange) url += `&date_range=${encodeURIComponent(dateRange)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        
        window.location.href = url;
    }

    function viewInvestmentDetails(investmentId) {
        currentInvestmentId = investmentId;
        const row = document.getElementById(`investment-${investmentId}`);
        if (!row) return;
        
        const cells = row.cells;
        const investmentData = {
            id: investmentId,
            investmentId: cells[0].textContent,
            userId: cells[1].querySelector('small').textContent.replace('ID: ', ''),
            username: cells[1].querySelector('strong').textContent,
            token: cells[2].textContent,
            amount: cells[3].textContent,
            dailyReturn: cells[4].textContent,
            progress: cells[5].querySelector('small').textContent,
            totalPaid: cells[6].textContent,
            remaining: cells[7].textContent,
            startDate: cells[8].textContent,
            lastPayout: cells[9].textContent,
            status: cells[10].textContent.trim()
        };
        
        document.getElementById('investmentDetailsContent').innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">Investment ID:</td>
                            <td class="fw-bold">${investmentData.investmentId}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">User:</td>
                            <td class="fw-bold">${investmentData.username}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">User ID:</td>
                            <td>${investmentData.userId}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Token:</td>
                            <td>${investmentData.token}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Amount:</td>
                            <td class="amount">${investmentData.amount}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Daily Return:</td>
                            <td class="amount-positive">${investmentData.dailyReturn}</td>
                        </tr>
                    </table>
                </div>
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">Progress:</td>
                            <td>${investmentData.progress}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Total Paid:</td>
                            <td class="amount-positive">${investmentData.totalPaid}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Remaining Payouts:</td>
                            <td class="amount-payout">${investmentData.remaining}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Start Date:</td>
                            <td>${investmentData.startDate}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Last Payout:</td>
                            <td>${investmentData.lastPayout}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Status:</td>
                            <td><span class="status-badge ${investmentData.status === 'ACTIVE' ? 'verified' : 'success'}">${investmentData.status}</span></td>
                        </tr>
                    </table>
                </div>
            </div>
        `;
        investmentDetailsModal.show();
    }

    async function processPayout(investmentId) {
        document.getElementById('confirmModalTitle').textContent = 'Process Payout';
        document.getElementById('confirmModalMessage').textContent = 'Process a single payout for this investment?';
        document.getElementById('confirmModalBtn').className = 'btn btn-success';
        document.getElementById('confirmModalBtn').textContent = 'Process';
        
        currentAction = 'processPayout';
        currentItemId = investmentId;
        confirmModal.show();
    }

    async function checkInvestmentPayouts(investmentId) {
        showLoading('Checking for missed payouts...');
        try {
            const result = await apiCall('check_investment_payouts', {
                investment_id: investmentId
            });
            
            if (result.success && result.missed > 0) {
                showToast(`Found ${result.missed} missed payouts. Processing...`, 'info');
                setTimeout(() => location.reload(), 2000);
            } else if (result.success) {
                showToast('No missed payouts found', 'success');
            }
        } catch (error) {
            hideLoading();
            showToast('Error checking payouts', 'error');
        }
    }

    async function forceCompleteInvestment(investmentId) {
        document.getElementById('confirmModalTitle').textContent = '⚠️ Force Complete Investment';
        document.getElementById('confirmModalMessage').innerHTML = `
            <p class="text-danger"><i class="bi bi-exclamation-triangle"></i> This will forcefully complete the investment and return principal.</p>
            <p>Are you absolutely sure?</p>
        `;
        document.getElementById('confirmModalBtn').className = 'btn btn-danger';
        document.getElementById('confirmModalBtn').textContent = 'Force Complete';
        
        currentAction = 'forceCompleteInvestment';
        currentItemId = investmentId;
        confirmModal.show();
    }

    // ========== PAYOUT ACTIONS ==========
    async function checkAllPayouts() {
        showLoading('Checking all payouts...');
        try {
            const response = await fetch(PAYOUT_STATS_URL, {
                method: 'GET',
                headers: {
                    'X-CSRFToken': CSRF_TOKEN,
                },
                credentials: 'same-origin'
            });
            const result = await response.json();
            hideLoading();
            
            if (result.success) {
                document.getElementById('dueTodayCount').textContent = result.due_for_payout || 0;
                document.getElementById('missedCount').textContent = result.missed || 0;
                document.getElementById('processedToday').textContent = result.payouts_last_24h || 0;
                
                if (document.getElementById('payoutDueToday')) {
                    document.getElementById('payoutDueToday').textContent = result.due_for_payout || 0;
                    document.getElementById('payoutMissed').textContent = result.missed || 0;
                    document.getElementById('payoutProcessed').textContent = result.payouts_last_24h || 0;
                }
                
                showToast(`Found ${result.due_for_payout} payouts due today`, 'info');
            } else {
                showToast(result.error || 'Error checking payouts', 'error');
            }
        } catch (error) {
            hideLoading();
            showToast('Error checking payouts: ' + error.message, 'error');
        }
    }

    async function catchUpAllPayouts() {
        document.getElementById('confirmModalTitle').textContent = 'Catch Up All Payouts';
        document.getElementById('confirmModalMessage').innerHTML = `
            <p>This will process ALL missed payouts for ALL users.</p>
            <p class="text-warning"><i class="bi bi-exclamation-triangle"></i> This may take a while for large systems.</p>
            <p>Continue?</p>
        `;
        document.getElementById('confirmModalBtn').className = 'btn btn-warning';
        document.getElementById('confirmModalBtn').textContent = 'Process All';
        
        currentAction = 'catchUpAllPayouts';
        confirmModal.show();
    }

    async function runDailyPayouts() {
        document.getElementById('confirmModalTitle').textContent = 'Run Daily Payouts';
        document.getElementById('confirmModalMessage').textContent = 'Process all due payouts for today?';
        document.getElementById('confirmModalBtn').className = 'btn btn-primary';
        document.getElementById('confirmModalBtn').textContent = 'Run';
        
        currentAction = 'runDailyPayouts';
        confirmModal.show();
    }

    async function checkExpiredInvestments() {
        document.getElementById('confirmModalTitle').textContent = 'Check Expired Investments';
        document.getElementById('confirmModalMessage').textContent = 'Mark all expired investments as completed?';
        document.getElementById('confirmModalBtn').className = 'btn btn-info';
        document.getElementById('confirmModalBtn').textContent = 'Check';
        
        currentAction = 'checkExpiredInvestments';
        confirmModal.show();
    }

    async function refreshPayoutStats() {
        try {
            const response = await fetch(PAYOUT_STATS_URL, {
                method: 'GET',
                headers: {
                    'X-CSRFToken': CSRF_TOKEN,
                },
                credentials: 'same-origin'
            });
            const result = await response.json();
            
            if (result.success) {
                document.getElementById('dueTodayCount').textContent = result.due_for_payout || 0;
                document.getElementById('missedCount').textContent = result.missed || 0;
                document.getElementById('processedToday').textContent = result.payouts_last_24h || 0;
                
                if (document.getElementById('payoutDueToday')) {
                    document.getElementById('payoutDueToday').textContent = result.due_for_payout || 0;
                    document.getElementById('payoutMissed').textContent = result.missed || 0;
                    document.getElementById('payoutProcessed').textContent = result.payouts_last_24h || 0;
                }
            }
        } catch (error) {
            console.error('Error refreshing payout stats:', error);
        }
    }

    // ========== USER ACTIONS ==========
    function filterUsers() {
        const search = document.getElementById('userSearch').value;
        const verified = document.getElementById('userVerifiedFilter').value;
        const banned = document.getElementById('userBannedFilter').value;
        const investor = document.getElementById('userInvestorFilter').value;
        
        let url = `?user_search=${search}&user_verified=${verified}&user_banned=${banned}&user_investor=${investor}#users`;
        window.location.href = url;
    }

    function viewUserDetails(userId) {
        currentUserId = userId;
        const row = document.getElementById(`user-${userId}`);
        if (!row) return;
        
        const cells = row.cells;
        const userData = {
            id: userId,
            username: cells[1].querySelector('strong').textContent,
            fullName: cells[1].querySelector('small').textContent,
            email: cells[2].textContent,
            phone: cells[3].textContent,
            balance: cells[4].textContent,
            locked: cells[5].textContent,
            available: cells[6].textContent,
            phoneVerified: cells[7].querySelectorAll('.badge')[0]?.classList.contains('bg-success') || false,
            idVerified: cells[7].querySelectorAll('.badge')[1]?.classList.contains('bg-success') || false,
            status: cells[8].textContent.trim(),
            joined: cells[9].textContent
        };
        
        document.getElementById('userDetailsContent').innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">User ID:</td>
                            <td class="fw-bold">#${userData.id}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Username:</td>
                            <td class="fw-bold">${userData.username}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Full Name:</td>
                            <td>${userData.fullName}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Email:</td>
                            <td>${userData.email}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Phone:</td>
                            <td>${userData.phone || '—'}</td>
                        </tr>
                    </table>
                </div>
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">Balance:</td>
                            <td class="amount">${userData.balance}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Locked Balance:</td>
                            <td class="amount-payout">${userData.locked}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Available Balance:</td>
                            <td class="${userData.available.includes('-') ? 'amount-negative' : 'amount-positive'}">${userData.available}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Verification:</td>
                            <td>
                                ${userData.phoneVerified ? '<span class="badge bg-success me-1">Phone</span>' : ''}
                                ${userData.idVerified ? '<span class="badge bg-success">ID</span>' : ''}
                            </td>
                        </tr>
                        <tr>
                            <td class="text-muted">Status:</td>
                            <td><span class="status-badge ${userData.status === 'Active' ? 'verified' : 'rejected'}">${userData.status}</span></td>
                        </tr>
                        <tr>
                            <td class="text-muted">Joined:</td>
                            <td>${userData.joined}</td>
                        </tr>
                    </table>
                </div>
            </div>
        `;
        userDetailsModal.show();
    }

    function adjustBalance(userId) {
        currentUserId = userId;
        document.getElementById('adjustUserId').value = userId;
        document.getElementById('adjustAmount').value = '';
        document.getElementById('adjustDescription').value = 'Credicted';
        document.getElementById('adjustType').value = 'add';
        adjustBalanceModal.show();
    }

    async function submitBalanceAdjustment() {
        const userId = document.getElementById('adjustUserId').value;
        const amount = document.getElementById('adjustAmount').value;
        const description = document.getElementById('adjustDescription').value;
        const adjustType = document.getElementById('adjustType').value;
        
        if (!amount) {
            showToast('Please enter an amount', 'error');
            return;
        }
        
        let actualAmount = parseFloat(amount);
        if (adjustType === 'subtract') {
            actualAmount = -actualAmount;
        }
        
        const result = await apiCall('adjust_balance', {
            user_id: userId,
            amount: actualAmount,
            description: description,
            adjust_type: adjustType
        });
        
        if (result.success) {
            adjustBalanceModal.hide();
            // Update the row
            const balanceCell = document.querySelector(`#user-${userId} td:nth-child(5)`);
            const lockedCell = document.querySelector(`#user-${userId} td:nth-child(6)`);
            const availableCell = document.querySelector(`#user-${userId} td:nth-child(7)`);
            
            if (balanceCell && result.new_balance !== undefined) {
                balanceCell.textContent = parseFloat(result.new_balance).toFixed(2) + ' KSH';
            }
            if (lockedCell && result.new_locked !== undefined) {
                lockedCell.textContent = parseFloat(result.new_locked).toFixed(2) + ' KSH';
            }
            if (balanceCell && lockedCell && availableCell) {
                const newBalance = parseFloat(result.new_balance || balanceCell.textContent);
                const newLocked = parseFloat(result.new_locked || lockedCell.textContent);
                const available = newBalance - newLocked;
                availableCell.textContent = available.toFixed(2) + ' KSH';
                availableCell.className = available < 0 ? 'amount-negative' : 'amount-positive';
            }
        }
    }

    async function toggleUserBan(userId, ban) {
        const reason = ban ? prompt('Enter ban reason:') : '';
        if (ban && !reason) return;
        
        document.getElementById('confirmModalTitle').textContent = ban ? 'Ban User' : 'Unban User';
        document.getElementById('confirmModalMessage').textContent = ban ? 
            `Ban user with reason: ${reason}` : 
            'Unban this user?';
        document.getElementById('confirmModalBtn').className = ban ? 'btn btn-danger' : 'btn btn-success';
        document.getElementById('confirmModalBtn').textContent = ban ? 'Ban' : 'Unban';
        
        currentAction = 'toggleUserBan';
        currentItemId = userId;
        currentItemData = { ban: ban, reason: reason };
        confirmModal.show();
    }

    function verifyUserKYC(userId) {
        switchTab('kyc');
        showToast('Navigate to KYC tab to verify documents', 'info');
    }

    // ========== TOKEN ACTIONS ==========
    function openTokenModal() {
        document.getElementById('tokenModalTitle').textContent = 'Create New Token';
        document.getElementById('tokenForm').reset();
        document.getElementById('tokenId').value = '';
        document.getElementById('tokenNumber').value = '';
        document.getElementById('tokenMinInvestment').value = '800';
        document.getElementById('tokenReturnDays').value = '12';
        document.getElementById('tokenMaxPurchases').value = '1';
        document.getElementById('tokenStatus').value = 'ACTIVE';
        document.getElementById('tokenColor').value = 'primary';
        tokenModal.show();
    }

    function editToken(tokenId) {
        const tokenRow = document.getElementById(`token-${tokenId}`);
        if (!tokenRow) return;
        
        const cells = tokenRow.cells;
        
        document.getElementById('tokenModalTitle').textContent = 'Edit Token';
        document.getElementById('tokenId').value = tokenId;
        document.getElementById('tokenName').value = cells[1].textContent.trim();
        document.getElementById('tokenDisplayName').value = cells[2].textContent.trim();
        document.getElementById('tokenNumber').value = cells[0].textContent.trim();
        document.getElementById('tokenMinInvestment').value = parseFloat(cells[3].textContent);
        document.getElementById('tokenDailyReturn').value = parseFloat(cells[4].textContent);
        document.getElementById('tokenReturnDays').value = parseInt(cells[5].textContent);
        document.getElementById('tokenStatus').value = cells[10].textContent.trim();
        document.getElementById('tokenMaxPurchases').value = parseInt(cells[9].textContent) || 1;
        
        const supplyText = cells[8].textContent.trim();
        const supply = supplyText.split('/')[1];
        document.getElementById('tokenTotalSupply').value = supply === '∞' ? '' : supply;
        
        tokenModal.show();
    }

    async function saveToken() {
        const formData = {
            name: document.getElementById('tokenName').value,
            display_name: document.getElementById('tokenDisplayName').value,
            token_number: document.getElementById('tokenNumber').value,
            minimum_investment: document.getElementById('tokenMinInvestment').value,
            daily_return: document.getElementById('tokenDailyReturn').value,
            return_days: document.getElementById('tokenReturnDays').value,
            status: document.getElementById('tokenStatus').value,
            max_purchases_per_user: document.getElementById('tokenMaxPurchases').value,
            total_supply: document.getElementById('tokenTotalSupply').value || '',
            description: document.getElementById('tokenDescription').value,
            icon: document.getElementById('tokenIcon').value || 'bi-coin',
            color: document.getElementById('tokenColor').value
        };
        
        const tokenId = document.getElementById('tokenId').value;
        const action = tokenId ? 'update_token' : 'create_token';
        
        if (tokenId) {
            formData.token_id = tokenId;
        }
        
        const result = await apiCall(action, formData);
        
        if (result.success) {
            tokenModal.hide();
            setTimeout(() => location.reload(), 1500);
        }
    }

    function viewTokenDetails(tokenId) {
        const tokenRow = document.getElementById(`token-${tokenId}`);
        if (!tokenRow) return;
        
        const cells = tokenRow.cells;
        showToast(`Token ${cells[1].textContent}: ${cells[3].textContent} min, ${cells[4].textContent} daily`, 'info');
    }

    async function toggleTokenStatus(tokenId) {
        const tokenRow = document.getElementById(`token-${tokenId}`);
        if (!tokenRow) return;
        
        const currentStatus = tokenRow.cells[10].textContent.trim();
        const newStatus = currentStatus === 'ACTIVE' ? 'INACTIVE' : 'ACTIVE';
        
        document.getElementById('confirmModalTitle').textContent = 'Toggle Token Status';
        document.getElementById('confirmModalMessage').textContent = `Change token status from ${currentStatus} to ${newStatus}?`;
        document.getElementById('confirmModalBtn').className = 'btn btn-warning';
        document.getElementById('confirmModalBtn').textContent = 'Toggle';
        
        currentAction = 'toggleTokenStatus';
        currentItemId = tokenId;
        currentItemData = { status: newStatus };
        confirmModal.show();
    }

    // ========== KYC ACTIONS ==========
    async function verifyKyc(profileId) {
        const phoneVerified = document.getElementById(`phoneVerify${profileId}`)?.checked || false;
        const idVerified = document.getElementById(`idVerify${profileId}`)?.checked || false;
        
        if (!phoneVerified && !idVerified) {
            showToast('Please select at least one verification option', 'warning');
            return;
        }
        
        document.getElementById('confirmModalTitle').textContent = 'Verify KYC';
        document.getElementById('confirmModalMessage').innerHTML = `
            Verify:
            ${phoneVerified ? '<br>- Phone' : ''}
            ${idVerified ? '<br>- ID' : ''}
        `;
        document.getElementById('confirmModalBtn').className = 'btn btn-success';
        document.getElementById('confirmModalBtn').textContent = 'Verify';
        
        currentAction = 'verifyKyc';
        currentItemId = profileId;
        currentItemData = { phone: phoneVerified, id: idVerified };
        confirmModal.show();
    }

    function rejectKyc(profileId) {
        const reason = prompt('Enter rejection reason:');
        if (!reason) return;
        
        document.getElementById('confirmModalTitle').textContent = 'Reject KYC';
        document.getElementById('confirmModalMessage').textContent = `Reject with reason: ${reason}`;
        document.getElementById('confirmModalBtn').className = 'btn btn-danger';
        document.getElementById('confirmModalBtn').textContent = 'Reject';
        
        currentAction = 'rejectKyc';
        currentItemId = profileId;
        currentItemData = { reason: reason };
        confirmModal.show();
    }

    // ========== TRANSACTIONS ACTIONS ==========
    function filterTransactions() {
        const type = document.getElementById('transactionTypeFilter').value;
        const status = document.getElementById('transactionStatusFilter').value;
        const dateRange = document.getElementById('transactionDateRange').value;
        const search = document.getElementById('transactionSearch').value;
        
        let url = `?transaction_type=${type}&transaction_status=${status}#transactions`;
        if (dateRange) url += `&date_range=${encodeURIComponent(dateRange)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        
        window.location.href = url;
    }

    function viewTransactionDetails(transactionId) {
        const row = document.getElementById(`transaction-${transactionId}`);
        if (!row) {
            showToast('Transaction not found', 'error');
            return;
        }
        
        const cells = row.cells;
        const transactionData = {
            id: transactionId,
            transactionId: cells[0].textContent,
            userId: cells[1].querySelector('small').textContent.replace('ID: ', ''),
            username: cells[1].querySelector('strong').textContent,
            type: cells[2].textContent.trim(),
            amount: cells[3].textContent,
            description: cells[4].querySelector('span')?.getAttribute('data-tooltip') || cells[4].textContent,
            date: cells[5].textContent,
            status: cells[6].textContent.trim()
        };
        
        document.getElementById('transactionDetailsContent').innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">Transaction ID:</td>
                            <td class="fw-bold">${transactionData.transactionId}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">User:</td>
                            <td class="fw-bold">${transactionData.username}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">User ID:</td>
                            <td>${transactionData.userId}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Type:</td>
                            <td><span class="status-badge ${transactionData.type === 'DEPOSIT' ? 'verified' : transactionData.type === 'WITHDRAWAL' ? 'processing' : 'info'}">${transactionData.type}</span></td>
                        </tr>
                    </table>
                </div>
                <div class="col-md-6">
                    <table class="table table-borderless">
                        <tr>
                            <td class="text-muted">Amount:</td>
                            <td class="${transactionData.amount.includes('-') ? 'amount-negative' : 'amount-positive'}">${transactionData.amount}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Description:</td>
                            <td>${transactionData.description}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Date:</td>
                            <td>${transactionData.date}</td>
                        </tr>
                        <tr>
                            <td class="text-muted">Status:</td>
                            <td><span class="status-badge ${transactionData.status === 'COMPLETED' ? 'verified' : transactionData.status === 'PENDING' ? 'pending' : 'rejected'}">${transactionData.status}</span></td>
                        </tr>
                    </table>
                </div>
            </div>
        `;
        transactionDetailsModal.show();
    }

    // ========== LOGS ACTIONS ==========
    function filterLogs() {
        const type = document.getElementById('logTypeFilter').value;
        const user = document.getElementById('logUserFilter').value;
        const dateRange = document.getElementById('logDateRange').value;
        const search = document.getElementById('logSearch').value;
        
        let url = `?log_type=${type}&log_user=${user}#logs`;
        if (dateRange) url += `&date_range=${encodeURIComponent(dateRange)}`;
        if (search) url += `&search=${encodeURIComponent(search)}`;
        
        window.location.href = url;
    }

    function clearLogs() {
        document.getElementById('confirmModalTitle').textContent = 'Clear Old Logs';
        document.getElementById('confirmModalMessage').textContent = 'Remove logs older than 30 days?';
        document.getElementById('confirmModalBtn').className = 'btn btn-danger';
        document.getElementById('confirmModalBtn').textContent = 'Clear';
        
        currentAction = 'clearLogs';
        confirmModal.show();
    }

    // ========== SETTINGS ACTIONS ==========
    async function saveSettings() {
        const settings = {
            config_min_deposit: document.getElementById('config_min_deposit')?.value || '800',
            config_mpesa_paybill: document.getElementById('config_mpesa_paybill')?.value || '123456',
            config_mpesa_account: document.getElementById('config_mpesa_account')?.value || 'INVEST',
            config_min_withdrawal: document.getElementById('config_min_withdrawal')?.value || '200',
            config_withdrawal_tax: document.getElementById('config_withdrawal_tax')?.value || '5',
            config_referral_commission: document.getElementById('config_referral_commission')?.value || '5',
            config_site_name: document.getElementById('config_site_name')?.value || 'XMR Investments',
            config_support_email: document.getElementById('config_support_email')?.value || 'support@example.com',
            config_support_phone: document.getElementById('config_support_phone')?.value || '0712345678',
            config_return_days: document.getElementById('config_return_days')?.value || '12',
            config_auto_payout: document.getElementById('config_auto_payout')?.value || '1',
            config_processing_time: document.getElementById('config_processing_time')?.value || '24',
            config_referral_bonus: document.getElementById('config_referral_bonus')?.value || '100',
            config_currency: document.getElementById('config_currency')?.value || 'KSH'
        };
        
        const result = await apiCall('update_settings', settings);
    }

    function resetSettings() {
        document.getElementById('confirmModalTitle').textContent = 'Reset Settings';
        document.getElementById('confirmModalMessage').textContent = 'Reset all settings to default values?';
        document.getElementById('confirmModalBtn').className = 'btn btn-warning';
        document.getElementById('confirmModalBtn').textContent = 'Reset';
        
        currentAction = 'resetSettings';
        confirmModal.show();
    }

    // ========== SYSTEM HEALTH ==========
    async function refreshSystemHealth() {
        try {
            const response = await fetch(SYSTEM_STATUS_URL, {
                method: 'GET',
                headers: {
                    'X-CSRFToken': CSRF_TOKEN,
                },
                credentials: 'same-origin'
            });
            const result = await response.json();
            
            if (result.success) {
                updateHealthIndicator(result.status);
                updateHealthDropdown(result.status);
            }
        } catch (error) {
            console.error('Error checking system health:', error);
        }
    }

    function updateHealthIndicator(status) {
        const indicator = document.querySelector('.health-indicator');
        if (indicator) {
            if (status?.database?.users_total > 0) {
                indicator.style.background = '#10b981';
            } else {
                indicator.style.background = '#ef4444';
            }
        }
    }

    function updateHealthDropdown(status) {
        const healthContent = document.getElementById('healthContent');
        if (!healthContent || !status) return;
        
        const issues = [];
        if (status.wallets?.wallets_with_negative > 0) issues.push(`${status.wallets.wallets_with_negative} negative balances`);
        if (status.investments?.overdue > 0) issues.push(`${status.investments.overdue} overdue payouts`);
        
        healthContent.innerHTML = `
            <div class="health-grid">
                <div class="health-item">
                    <div class="health-status ${status.database?.users_total > 0 ? 'healthy' : 'warning'}"></div>
                    <div>
                        <small>Database</small>
                        <div>${status.database?.users_total || 0} users</div>
                    </div>
                </div>
                <div class="health-item">
                    <div class="health-status ${status.wallets?.wallets_with_negative === 0 ? 'healthy' : 'critical'}"></div>
                    <div>
                        <small>Wallets</small>
                        <div>${status.wallets?.total_wallets || 0} total</div>
                    </div>
                </div>
                <div class="health-item">
                    <div class="health-status ${status.investments?.overdue === 0 ? 'healthy' : 'warning'}"></div>
                    <div>
                        <small>Investments</small>
                        <div>${status.investments?.active || 0} active</div>
                    </div>
                </div>
                <div class="health-item">
                    <div class="health-status ${status.transactions?.pending === 0 ? 'healthy' : 'warning'}"></div>
                    <div>
                        <small>Transactions</small>
                        <div>${status.transactions?.pending || 0} pending</div>
                    </div>
                </div>
            </div>
            ${issues.length > 0 ? `
                <div class="mt-3 p-2 bg-danger bg-opacity-10 rounded">
                    <small class="text-danger">⚠️ Issues: ${issues.join(', ')}</small>
                </div>
            ` : ''}
        `;
    }

    function openSystemStatus() {
        systemStatusModal.show();
        refreshSystemStatus();
    }

    async function refreshSystemStatus() {
        showLoading('Checking system status...');
        try {
            const response = await fetch(SYSTEM_STATUS_URL, {
                method: 'GET',
                headers: {
                    'X-CSRFToken': CSRF_TOKEN,
                },
                credentials: 'same-origin'
            });
            const result = await response.json();
            hideLoading();
            
            if (result.success) {
                displaySystemStatus(result.status);
            } else {
                showToast('Error getting system status', 'error');
            }
        } catch (error) {
            hideLoading();
            showToast('Error: ' + error.message, 'error');
        }
    }

    function displaySystemStatus(status) {
        const content = document.getElementById('systemStatusContent');
        if (!content) return;
        
        content.innerHTML = `
            <div class="row">
                <div class="col-md-6">
                    <h6>Database</h6>
                    <table class="table table-sm">
                        <tr><td>Total Users:</td><td class="fw-bold">${status.database?.users_total || 0}</td></tr>
                        <tr><td>Active (24h):</td><td>${status.database?.users_active_last_day || 0}</td></tr>
                        <tr><td>New Today:</td><td>${status.database?.users_active_last_hour || 0}</td></tr>
                    </table>
                    
                    <h6 class="mt-3">Wallets</h6>
                    <table class="table table-sm">
                        <tr><td>Total Wallets:</td><td>${status.wallets?.total_wallets || 0}</td></tr>
                        <tr><td>Available Balance:</td><td class="amount-positive">${status.wallets?.total_available_balance?.toLocaleString() || 0} KSH</td></tr>
                        <tr><td>Locked Balance:</td><td class="amount-payout">${status.wallets?.total_locked_balance?.toLocaleString() || 0} KSH</td></tr>
                        <tr><td>Negative Balances:</td><td class="${status.wallets?.wallets_with_negative > 0 ? 'text-danger' : 'text-success'}">${status.wallets?.wallets_with_negative || 0}</td></tr>
                    </table>
                </div>
                <div class="col-md-6">
                    <h6>Investments</h6>
                    <table class="table table-sm">
                        <tr><td>Active:</td><td class="text-success">${status.investments?.active || 0}</td></tr>
                        <tr><td>Completed:</td><td>${status.investments?.completed || 0}</td></tr>
                        <tr><td>Total Invested:</td><td class="amount">${status.investments?.total_invested?.toLocaleString() || 0} KSH</td></tr>
                        <tr><td>Due Today:</td><td class="text-warning">${status.investments?.due_for_payout || 0}</td></tr>
                        <tr><td>Overdue:</td><td class="text-danger">${status.investments?.overdue || 0}</td></tr>
                    </table>
                    
                    <h6 class="mt-3">Pending Actions</h6>
                    <table class="table table-sm">
                        <tr><td>Deposits:</td><td class="text-warning">${status.deposits?.pending || 0}</td></tr>
                        <tr><td>Withdrawals:</td><td class="text-info">${status.withdrawals?.pending || 0}</td></tr>
                        <tr><td>KYC:</td><td class="text-primary">${status.kyc?.pending || 0}</td></tr>
                        <tr><td>Transactions:</td><td>${status.transactions?.pending || 0}</td></tr>
                    </table>
                </div>
            </div>
        `;
    }

    // ========== MAINTENANCE ACTIONS ==========
    function openMaintenanceMenu() {
        maintenanceModal.show();
    }

    async function runMaintenance(action) {
        maintenanceModal.hide();
        
        const actions = {
            'fix_wallets': {
                title: 'Fix All Wallets',
                message: 'This will recalculate and fix all wallet balances. Continue?',
                url: FIX_WALLETS_URL,
                method: 'POST'
            },
            'check_expired': {
                title: 'Check Expired Investments',
                message: 'Mark all expired investments as completed?',
                url: '{% url "XMR:admin_check_expired" %}',
                method: 'POST'
            },
            'run_payouts': {
                title: 'Run Daily Payouts',
                message: 'Process all due payouts?',
                url: '{% url "XMR:admin_trigger_payout" %}',
                method: 'POST'
            },
            'catch_up_payouts': {
                title: 'Catch Up All Payouts',
                message: 'Process ALL missed payouts for ALL users?',
                url: CATCH_UP_URL,
                method: 'POST'
            },
            'clear_logs': {
                title: 'Clear Old Logs',
                message: 'Remove logs older than 30 days?',
                action: 'clear_logs'
            }
        };
        
        const config = actions[action];
        if (!config) return;
        
        document.getElementById('confirmModalTitle').textContent = config.title;
        document.getElementById('confirmModalMessage').textContent = config.message;
        document.getElementById('confirmModalBtn').className = 'btn btn-warning';
        document.getElementById('confirmModalBtn').textContent = 'Execute';
        
        currentAction = action;
        confirmModal.show();
    }

    // ========== EXPORT ACTIONS ==========
    function exportData(type) {
        const urls = {
            'users': EXPORT_USERS_URL,
            'transactions': EXPORT_TRANSACTIONS_URL,
            'deposits': '{% url "XMR:export_deposits_csv" %}',
            'withdrawals': '{% url "XMR:export_withdrawals_csv" %}',
            'investments': '{% url "XMR:export_investments_csv" %}',
            'tokens': '{% url "XMR:export_tokens_csv" %}',
            'kyc': '{% url "XMR:export_kyc_csv" %}',
            'logs': '{% url "XMR:export_logs_csv" %}'
        };
        
        const url = urls[type];
        if (url) {
            window.location.href = url;
            showToast(`Exporting ${type}...`, 'info');
        }
    }

    // ========== UTILITY FUNCTIONS ==========
    function viewScreenshot(imageUrl) {
        if (imageUrl && imageUrl !== 'None' && imageUrl !== '') {
            viewImage(imageUrl);
        } else {
            showToast('No screenshot available', 'info');
        }
    }

    function viewImage(imageUrl) {
        document.getElementById('previewImage').src = imageUrl;
        document.getElementById('downloadImageBtn').href = imageUrl;
        imagePreviewModal.show();
    }

    // ========== CONFIRMATION HANDLER ==========
    document.getElementById('confirmModalBtn')?.addEventListener('click', async function() {
        confirmModal.hide();
        
        if (currentAction === 'verifyDeposit') {
            const result = await apiCall('verify_deposit', { deposit_id: currentItemId });
            if (result.success) {
                document.getElementById(`deposit-${currentItemId}`)?.remove();
                updateNotificationCounts();
            }
        } else if (currentAction === 'rejectDeposit') {
            const result = await apiCall('reject_deposit', {
                deposit_id: currentItemId,
                reason: currentItemData?.reason || ''
            });
            if (result.success) {
                document.getElementById(`deposit-${currentItemId}`)?.remove();
                updateNotificationCounts();
            }
        } else if (currentAction === 'processWithdrawal') {
            const result = await apiCall('process_withdrawal', {
                withdrawal_id: currentItemId
            });
            if (result.success) {
                const row = document.getElementById(`withdrawal-${currentItemId}`);
                if (row) {
                    const statusCell = row.querySelector('.status-badge');
                    if (statusCell) {
                        statusCell.className = 'status-badge processing';
                        statusCell.textContent = 'PROCESSING';
                    }
                }
            }
        } else if (currentAction === 'completeWithdrawal') {
            const result = await apiCall('complete_withdrawal', {
                withdrawal_id: currentItemId,
                transaction_code: currentItemData?.code || ''
            });
            if (result.success) {
                const row = document.getElementById(`withdrawal-${currentItemId}`);
                if (row) {
                    row.remove();
                    updateNotificationCounts();
                }
            }
        } else if (currentAction === 'rejectWithdrawal') {
            const result = await apiCall('reject_withdrawal', {
                withdrawal_id: currentItemId,
                reason: currentItemData?.reason || ''
            });
            if (result.success) {
                const row = document.getElementById(`withdrawal-${currentItemId}`);
                if (row) {
                    row.remove();
                    updateNotificationCounts();
                }
            }
        } else if (currentAction === 'processPayout') {
            const result = await apiCall('process_payout', {
                investment_id: currentItemId
            });
            if (result.success) {
                showToast('Payout processed successfully', 'success');
                setTimeout(() => location.reload(), 1500);
            }
        } else if (currentAction === 'forceCompleteInvestment') {
            const result = await apiCall('force_complete_investment', {
                investment_id: currentItemId
            });
            if (result.success) {
                showToast('Investment force completed', 'success');
                setTimeout(() => location.reload(), 1500);
            }
        } else if (currentAction === 'toggleUserBan') {
            const result = await apiCall('toggle_user_ban', {
                user_id: currentItemId,
                reason: currentItemData?.reason || ''
            });
            if (result.success) {
                const row = document.getElementById(`user-${currentItemId}`);
                if (row) {
                    const statusCell = row.querySelectorAll('td')[8];
                    const banBtn = row.querySelector('.btn-icon.danger, .btn-icon.success');
                    
                    if (currentItemData?.ban) {
                        statusCell.innerHTML = '<span class="status-badge rejected">Banned</span>';
                        if (banBtn) {
                            banBtn.className = 'btn-icon success';
                            banBtn.innerHTML = '<i class="bi bi-unlock"></i>';
                            banBtn.setAttribute('onclick', `toggleUserBan(${currentItemId}, false)`);
                            banBtn.setAttribute('data-tooltip', 'Unban');
                        }
                    } else {
                        statusCell.innerHTML = '<span class="status-badge verified">Active</span>';
                        if (banBtn) {
                            banBtn.className = 'btn-icon danger';
                            banBtn.innerHTML = '<i class="bi bi-lock"></i>';
                            banBtn.setAttribute('onclick', `toggleUserBan(${currentItemId}, true)`);
                            banBtn.setAttribute('data-tooltip', 'Ban');
                        }
                    }
                }
            }
        } else if (currentAction === 'toggleTokenStatus') {
            const result = await apiCall('update_token', {
                token_id: currentItemId,
                status: currentItemData?.status
            });
            if (result.success) {
                setTimeout(() => location.reload(), 1500);
            }
        } else if (currentAction === 'verifyKyc') {
            if (currentItemData?.phone && currentItemData?.id) {
                await apiCall('verify_kyc_all', { profile_id: currentItemId });
            } else if (currentItemData?.phone) {
                await apiCall('verify_kyc_phone', { profile_id: currentItemId });
            } else if (currentItemData?.id) {
                await apiCall('verify_kyc_id', { profile_id: currentItemId });
            }
            document.getElementById(`kyc-${currentItemId}`)?.remove();
            updateNotificationCounts();
        } else if (currentAction === 'rejectKyc') {
            showToast('KYC rejected: ' + (currentItemData?.reason || ''), 'warning');
            document.getElementById(`kyc-${currentItemId}`)?.remove();
            updateNotificationCounts();
        } else if (currentAction === 'catchUpAllPayouts') {
            showLoading('Processing all payouts...');
            try {
                const response = await fetch(CATCH_UP_URL, {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': CSRF_TOKEN,
                    },
                    credentials: 'same-origin'
                });
                const result = await response.json();
                hideLoading();
                if (result.success) {
                    showToast(`✅ Processed ${result.processed} payouts for ${result.users} users`, 'success');
                    setTimeout(() => location.reload(), 2000);
                } else {
                    showToast(result.error || 'Error processing payouts', 'error');
                }
            } catch (error) {
                hideLoading();
                showToast('Network error: ' + error.message, 'error');
            }
        } else if (currentAction === 'runDailyPayouts') {
            showLoading('Running daily payouts...');
            try {
                const response = await fetch('{% url "XMR:admin_trigger_payout" %}', {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': CSRF_TOKEN,
                    },
                    credentials: 'same-origin'
                });
                const result = await response.json();
                hideLoading();
                if (result.success) {
                    showToast(`Processed ${result.processed} payouts`, 'success');
                    refreshPayoutStats();
                } else {
                    showToast(result.error || 'Error', 'error');
                }
            } catch (error) {
                hideLoading();
                showToast('Error: ' + error.message, 'error');
            }
        } else if (currentAction === 'checkExpiredInvestments') {
            showLoading('Checking expired investments...');
            try {
                const response = await fetch('{% url "XMR:admin_check_expired" %}', {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': CSRF_TOKEN,
                    },
                    credentials: 'same-origin'
                });
                const result = await response.json();
                hideLoading();
                if (result.success) {
                    showToast(`Marked ${result.count} investments as completed`, 'success');
                } else {
                    showToast(result.error || 'Error', 'error');
                }
            } catch (error) {
                hideLoading();
                showToast('Error: ' + error.message, 'error');
            }
        } else if (currentAction === 'fix_wallets') {
            showLoading('Fixing wallets...');
            try {
                const response = await fetch(FIX_WALLETS_URL, {
                    method: 'POST',
                    headers: {
                        'X-CSRFToken': CSRF_TOKEN,
                    },
                    credentials: 'same-origin'
                });
                const result = await response.json();
                hideLoading();
                if (result.success) {
                    showToast(result.message, 'success');
                } else {
                    showToast(result.error || 'Error', 'error');
                }
            } catch (error) {
                hideLoading();
                showToast('Error: ' + error.message, 'error');
            }
        } else if (currentAction === 'clearLogs') {
            showToast('Logs cleared', 'success');
        } else if (currentAction === 'resetSettings') {
            document.getElementById('config_min_deposit').value = '800';
            document.getElementById('config_mpesa_paybill').value = '123456';
            document.getElementById('config_mpesa_account').value = 'INVEST';
            document.getElementById('config_min_withdrawal').value = '200';
            document.getElementById('config_withdrawal_tax').value = '5';
            document.getElementById('config_referral_commission').value = '5';
            document.getElementById('config_site_name').value = 'XMR Investments';
            document.getElementById('config_support_email').value = 'support@example.com';
            document.getElementById('config_support_phone').value = '0712345678';
            document.getElementById('config_return_days').value = '12';
            document.getElementById('config_auto_payout').value = '1';
            document.getElementById('config_processing_time').value = '24';
            document.getElementById('config_referral_bonus').value = '100';
            document.getElementById('config_currency').value = 'KSH';
            showToast('Settings reset to defaults', 'success');
        }
        
        currentAction = null;
        currentItemId = null;
        currentItemData = null;
    });

    // ========== KEYBOARD SHORTCUTS ==========
    document.addEventListener('keydown', function(e) {
        // Ctrl+1 through Ctrl+9 for tabs
        if (e.ctrlKey && e.key >= '1' && e.key <= '9') {
            e.preventDefault();
            const tabs = ['dashboard', 'deposits', 'withdrawals', 'investments', 'payouts', 'users', 'tokens', 'kyc', 'transactions', 'logs'];
            const index = parseInt(e.key) - 1;
            if (tabs[index]) {
                switchTab(tabs[index]);
            }
        }
        
        // Escape to close modals
        if (e.key === 'Escape') {
            const openModals = document.querySelectorAll('.modal.show');
            openModals.forEach(modal => {
                const modalInstance = bootstrap.Modal.getInstance(modal);
                if (modalInstance) {
                    modalInstance.hide();
                }
            });
        }
        
        // Ctrl+R to refresh current tab
        if (e.ctrlKey && e.key === 'r') {
            e.preventDefault();
            if (currentTab === 'dashboard') {
                refreshDashboard();
            } else if (currentTab === 'payouts') {
                refreshPayoutStats();
            } else if (currentTab === 'logs') {
                filterLogs();
            } else {
                location.reload();
            }
        }
        
        // Ctrl+F to focus search
        if (e.ctrlKey && e.key === 'f') {
            e.preventDefault();
            const searchInput = document.querySelector('.search-box input');
            if (searchInput) {
                searchInput.focus();
            }
        }
    });

    // ========== AUTO-REFRESH ==========
    setInterval(function() {
        if (currentTab === 'deposits') {
            // Auto-refresh deposits data
        } else if (currentTab === 'withdrawals') {
            // Auto-refresh withdrawals data
        } else if (currentTab === 'payouts' || currentTab === 'dashboard') {
            refreshPayoutStats();
        }
    }, 30000);
