// static/js/dashboard.js

document.addEventListener('DOMContentLoaded', () => {
    // --- DOM Elements ---
    const totalUsersEl = document.getElementById('total-users');
    const onlineUsersEl = document.getElementById('online-users');
    const activatedUsersEl = document.getElementById('activated-users');
    const groupChartCtx = document.getElementById('group-chart').getContext('2d');
    const versionChartCtx = document.getElementById('version-chart').getContext('2d');

    let groupChartInstance;
    let versionChartInstance;

    // --- Data Processing and Rendering ---
    const processAndRenderData = (data) => {
        let totalUsers = 0;
        let onlineCount = 0;
        let activatedCount = 0;
        const groupCounts = {};
        const versionCounts = {};

        data.forEach(group => {
            groupCounts[group.group_name] = group.users.length;
            group.users.forEach(user => {
                totalUsers++;
                if (user.is_online) onlineCount++;
                if (user.is_activated) activatedCount++;

                // Aggregate version data
                versionCounts[user.version] = (versionCounts[user.version] || 0) + 1;
            });
        });

        // --- Update KPI Cards ---
        totalUsersEl.textContent = totalUsers.toLocaleString();
        onlineUsersEl.textContent = onlineCount.toLocaleString();
        activatedUsersEl.textContent = activatedCount.toLocaleString();

        // --- Render Charts ---
        renderGroupChart(groupCounts);
        renderVersionChart(versionCounts);
    };

    const renderGroupChart = (groupData) => {
        if (groupChartInstance) {
            groupChartInstance.destroy();
        }
        groupChartInstance = new Chart(groupChartCtx, {
            type: 'bar',
            data: {
                labels: Object.keys(groupData),
                datasets: [{
                    label: 'จำนวนผู้ใช้',
                    data: Object.values(groupData),
                    backgroundColor: 'rgba(59, 130, 246, 0.5)',
                    borderColor: 'rgba(59, 130, 246, 1)',
                    borderWidth: 1
                }]
            },
            options: {
                responsive: true,
                plugins: { legend: { display: false } },
                scales: {
                    y: { beginAtZero: true, ticks: { precision: 0 } }
                }
            }
        });
    };

    const renderVersionChart = (versionData) => {
        if (versionChartInstance) {
            versionChartInstance.destroy();
        }
        const sortedVersions = Object.entries(versionData).sort((a, b) => a[0].localeCompare(b[0]));

        versionChartInstance = new Chart(versionChartCtx, {
            type: 'doughnut',
            data: {
                labels: sortedVersions.map(v => v[0]),
                datasets: [{
                    label: 'Versions',
                    data: sortedVersions.map(v => v[1]),
                    backgroundColor: [
                        'rgba(239, 68, 68, 0.7)',
                        'rgba(245, 158, 11, 0.7)',
                        'rgba(16, 185, 129, 0.7)',
                        'rgba(14, 165, 233, 0.7)',
                        'rgba(139, 92, 246, 0.7)',
                    ],
                    hoverOffset: 4
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        position: 'top',
                    }
                }
            }
        });
    };

    // --- Main Data Fetching ---
    const fetchData = async () => {
        try {
            const response = await fetch('/ai-camera/api/users');
            if (!response.ok) throw new Error('Network response was not ok');
            const allGroupsData = await response.json();
            processAndRenderData(allGroupsData);
        } catch (error) {
            console.error("Error fetching dashboard data:", error);
            // You can display an error message on the dashboard if needed
        }
    };

    fetchData();
});